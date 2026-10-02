from __future__ import annotations

import binascii
import json

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
