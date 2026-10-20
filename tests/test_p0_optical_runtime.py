from __future__ import annotations

import binascii
import json

from easychange.remote import optical_protocol
from easychange.core.command_service import CommandService
from easychange.core.process_service import ProcessService
from easychange.core.result_store import DurableResultStore
from easychange.core.runtime_service import ProjectRuntimeService
from easychange.core.workspace import Workspace


def test_optical_chunks_returns_only_requested_indexes_with_crc(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        sequence = "QOPTICALB1"
        result = {
            "ok": True,
            "command": "read",
            "data": {
                "lines": [
                    f"{i:04d}|public string Value{i} = \"{i * 7919:08d}\";"
                    for i in range(600)
                ],
            },
            "error": None,
            "code": None,
            "command_id": "C1",
            "duration_ms": 3,
            "sequence": sequence,
        }
        service.result_store.mark_running(
            sequence, "a" * 64, ":read sample.cs", mutation=False
        )
        service.result_store.complete(
            sequence, "a" * 64, ":read sample.cs", result
        )
        response = service._execute_structured({
            "op": "operation",
            "type": "optical_chunks",
            "target_sequence": sequence,
            "chunk_indexes": [0, 2],
        })
        assert response.ok is True
        data = response.data
        assert data["requested"] == [0, 2]
        assert [packet["i"] for packet in data["packets"]] == [0, 2]
        assert data["total_chunks"] > 2
        for packet in data["packets"]:
            piece = str(packet["d"])
            expected = f"{binascii.crc32(piece.encode('ascii')) & 0xffffffff:08x}"
            assert packet["x"] == expected
            assert data["chunk_crc32"][str(packet["i"])] == expected
    finally:
        service.close()


def test_selective_optical_recovery_reuses_prepared_result(tmp_path, monkeypatch):
    service = CommandService(Workspace.open(tmp_path))
    try:
        sequence = "QOPTICALCACHE1"
        result = {
            "ok": True,
            "command": "read",
            "data": {"text": "public source line\n" * 8000},
            "error": None,
            "code": None,
            "command_id": "C-CACHE",
            "duration_ms": 2,
            "sequence": sequence,
        }
        service.result_store.mark_running(
            sequence, "d" * 64, ":read sample.txt", mutation=False
        )
        service.result_store.complete(
            sequence, "d" * 64, ":read sample.txt", result
        )
        original_prepare = optical_protocol.prepare_optical_result
        calls = 0

        def counted_prepare(value, **kwargs):
            nonlocal calls
            calls += 1
            return original_prepare(value, **kwargs)

        monkeypatch.setattr(optical_protocol, "prepare_optical_result", counted_prepare)
        responses = []
        for index in (0, 1, 2):
            responses.append(service._execute_structured({
                "op": "operation",
                "type": "optical_chunk",
                "target_sequence": sequence,
                "chunk_index": index,
            }))

        assert all(response.ok for response in responses)
        assert [response.data["packet"]["i"] for response in responses] == [0, 1, 2]
        assert calls == 1
        assert len(service._optical_encoding_cache) == 1
        assert sum(service._optical_encoding_cache_sizes.values()) <= 8 * 1024 * 1024
    finally:
        service.close()




def test_optical_chunks_rejects_multi_page_recovery_request(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        sequence = "QOPTICALLIMIT1"
        result = {
            "ok": True,
            "command": "read",
            "data": {"text": "large-result-" * 1200},
            "error": None,
            "code": None,
            "command_id": "C-LIMIT",
            "duration_ms": 1,
            "sequence": sequence,
        }
        service.result_store.mark_running(
            sequence, "c" * 64, ":read large.cs", mutation=False
        )
        service.result_store.complete(
            sequence, "c" * 64, ":read large.cs", result
        )

        response = service._execute_structured({
            "op": "operation",
            "type": "optical_chunks",
            "target_sequence": sequence,
            "chunk_indexes": list(range(17)),
        })

        assert response.ok is False
        assert response.code == "INVALID_RESULT_CHUNK"
        assert "CHUNK_INDEXES_REQUIRES_1_TO_16_ITEMS" in str(response.error)
    finally:
        service.close()

def test_smart_test_summary_parses_dotnet_and_pytest_counts():
    summary = ProjectRuntimeService._test_summary([
        {
            "returncode": 0,
            "stdout": "Passed: 12\nFailed: 0\nSkipped: 2\n",
            "stderr": "",
            "diagnostics": "",
        },
        {
            "returncode": 1,
            "stdout": "3 passed, 1 failed, 4 skipped",
            "stderr": "error CS1002: ; expected",
            "diagnostics": "error CS1002: ; expected",
        },
    ])
    assert summary["finished"] is True
    assert summary["return_code"] == 1
    assert summary["tests_passed"] == 15
    assert summary["tests_failed"] == 1
    assert summary["tests_skipped"] == 6
    assert summary["test_failures_count"] == 1
    assert summary["build_errors_count"] >= 1


def test_result_header_persists_test_summary_without_stdout(tmp_path):
    store = DurableResultStore(tmp_path / "results")
    sequence = "QTESTSUM1"
    store.mark_running(sequence, "b" * 64, ":test-smart", mutation=False)
    summary = {
        "finished": True,
        "return_code": 0,
        "tests_passed": 23,
        "tests_failed": 0,
        "tests_skipped": 1,
        "build_errors_count": 0,
        "test_failures_count": 0,
    }
    store.complete(
        sequence,
        "b" * 64,
        ":test-smart",
        {
            "ok": True,
            "command": "test_smart",
            "data": {
                "passed": True,
                "test_summary": summary,
                "results": [{"stdout": "x" * 20000}],
            },
            "code": None,
            "sequence": sequence,
        },
    )
    header = store.header(sequence)
    assert header["summary"] == summary
    assert header["return_code"] == 0
    assert "stdout" not in header
