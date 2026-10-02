from __future__ import annotations

import json

from easychange.core.command_service import CommandService
from easychange.core.result_store import DurableResultStore
from easychange.core.workspace import Workspace


def test_mutation_receipt_is_definitive_and_auditable(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    try:
        result = service._execute_structured({
            "op": "batch",
            "operations": [{
                "type": "replace_exact",
                "path": "sample.txt",
                "old_text": "beta",
                "new_text": "BETA",
                "expected_occurrences": 1,
            }],
        })
        assert result.ok is True
        receipt = result.data["mutation_receipt"]
        assert receipt["state"] == "COMMITTED"
        assert receipt["lifecycle_state"] == "APPLIED"
        assert receipt["operations_requested"] == 1
        assert receipt["operations_applied"] == 1
        assert receipt["matched_occurrences"] == 1
        assert receipt["journal_ids"]
        assert receipt["validation_result"]["ok"] is True
        assert receipt["files"][0]["before_hash"]
        assert receipt["files"][0]["after_hash"]
        assert receipt["files"][0]["changed_ranges"]
    finally:
        service.close()


def test_result_header_is_small_and_durable(tmp_path):
    store = DurableResultStore(tmp_path / "results")
    sequence = "QHEADER01"
    running = store.mark_running(sequence, "a" * 64, ":state", mutation=False)
    assert running["execution_state"] == "STARTED"
    result = {
        "ok": True,
        "command": "test_smart",
        "data": {"passed": True, "results": [{"stdout": "x" * 20000}]},
        "code": None,
        "sequence": sequence,
        "command_id": "C9",
    }
    store.complete(sequence, "a" * 64, ":state", result)
    header = store.header(sequence)
    assert header["sequence"] == sequence
    assert header["finished"] is True
    assert header["success"] is True
    assert header["payload_length"] > 10000
    assert header["sha256"]
    assert header["execution_id"]
    assert header["command_id"] == "C9"
    assert "result" not in header


def test_structured_result_header_recovery_operation(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        sequence = "QHDRREC1"
        service.result_store.mark_running(sequence, "b" * 64, ":state", mutation=False)
        service.result_store.complete(
            sequence,
            "b" * 64,
            ":state",
            {"ok": True, "command": "state", "data": {"state": "READY"}, "sequence": sequence},
        )
        result = service._execute_structured({
            "op": "operation",
            "type": "result_header",
            "sequence": sequence,
        })
        assert result.ok is True
        assert result.data["finished"] is True
        assert result.data["success"] is True
    finally:
        service.close()


def test_same_sequence_mutation_replays_receipt_without_duplicate_write(tmp_path, monkeypatch):
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    try:
        writes = 0
        original_write = service._write

        def counted_write(*args, **kwargs):
            nonlocal writes
            writes += 1
            return original_write(*args, **kwargs)

        monkeypatch.setattr(service, "_write", counted_write)
        operation = {
            "op": "operation",
            "type": "replace_exact",
            "path": "sample.txt",
            "old_text": "beta",
            "new_text": "BETA",
            "expected_occurrences": 1,
        }
        wire = ":j1 " + json.dumps(operation, separators=(",", ":"))
        first = service.execute(f":ec QMUTATE1 {wire}")
        second = service.execute(f":ec QMUTATE1 {wire}")
        assert first.ok is True
        assert second.ok is True
        assert writes == 1
        assert second.data["duplicate"] is True
        receipt = second.data["mutation_receipt"]
        assert receipt["lifecycle_state"] == "APPLIED"
        status = service.result_store.status("QMUTATE1")
        assert status["mutation_state"] == "APPLIED"
    finally:
        service.close()
