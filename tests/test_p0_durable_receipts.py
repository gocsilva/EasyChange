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



def test_cross_instance_durable_claim_has_single_execution_owner(tmp_path):
    import pytest

    folder=tmp_path / "claims"
    primary=DurableResultStore(folder)
    secondary=DurableResultStore(folder)
    first, acquired=primary.claim_running(
        "QCLAIM01", "a"*64, ":mutation", mutation=True)
    replay, acquired_again=secondary.claim_running(
        "QCLAIM01", "a"*64, ":mutation", mutation=True)
    assert acquired is True
    assert acquired_again is False
    assert replay["execution_id"] == first["execution_id"]
    assert replay["mutation_state"] == "STARTED"
    # The historical mark_running() API still returns the receipt object.
    assert primary.mark_running(
        "QCLAIM01", "a"*64, ":mutation", mutation=True)["execution_id"] == first["execution_id"]
    with pytest.raises(ValueError, match="RESULT_SEQUENCE_COMMAND_HASH_MISMATCH"):
        secondary.claim_running("QCLAIM01", "b"*64, ":different", mutation=True)


def test_racing_sequence_claim_must_not_execute_second_mutation(tmp_path, monkeypatch):
    target=tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    service=CommandService(Workspace.open(tmp_path))
    try:
        operation={"op":"operation","type":"replace_exact",
                   "path":"sample.txt","old_text":"beta",
                   "new_text":"BETA","expected_occurrences":1}
        original=service.result_store.claim_running
        raced=[]

        def competing_claim(sequence, command_hash, command, *, mutation):
            # Simulate another process winning the file lock after our first
            # empty get(), but before this service reaches claim_running().
            other, was_new=original(
                sequence, command_hash, command, mutation=mutation)
            assert was_new is True
            replay, was_new=original(
                sequence, command_hash, command, mutation=mutation)
            assert replay["execution_id"] == other["execution_id"]
            raced.append(was_new)
            return replay, was_new

        monkeypatch.setattr(service.result_store,"claim_running",competing_claim)
        def forbidden_execution(*args, **kwargs):
            raise AssertionError("duplicate filesystem mutation executed")
        monkeypatch.setattr(service,"_execute_structured",forbidden_execution)
        reply=service.execute(":ec QRACE01 :j1 "+json.dumps(operation))
        assert reply.code == "RUNNING"
        assert raced == [False]
        assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"
    finally:
        service.close()


def test_racing_sequence_with_different_command_is_conflict(tmp_path, monkeypatch):
    service=CommandService(Workspace.open(tmp_path))
    try:
        operation={"op":"operation","type":"replace_exact",
                   "path":"missing.txt","old_text":"old","new_text":"new"}
        original=service.result_store.claim_running

        def competing_claim(sequence, command_hash, command, *, mutation):
            record, acquired=original(
                sequence, "b"*64, ":other-command", mutation=True)
            assert acquired is True and record["state"] == "RUNNING"
            return original(sequence, command_hash, command, mutation=mutation)

        monkeypatch.setattr(service.result_store,"claim_running",competing_claim)
        monkeypatch.setattr(
            service,"_execute_structured",
            lambda *args,**kwargs: (_ for _ in ()).throw(
                AssertionError("conflicting command executed")))
        reply=service.execute(":ec QRACE02 :j1 "+json.dumps(operation))
        assert reply.code == "SEQUENCE_CONFLICT"
        assert service.result_store.get("QRACE02")["command_hash"] == "b"*64
    finally:
        service.close()


def test_compressed_receipt_file_read_is_bounded(tmp_path, monkeypatch):
    import io
    from pathlib import Path
    import pytest
    import easychange.core.result_store as result_module

    target = tmp_path / "QBOUNDED.json.zlib"
    observed = []

    class RecordedStream(io.BytesIO):
        def read(self, limit=-1):
            observed.append(limit)
            return super().read(limit)

    stream = RecordedStream(b"X" * 30)
    monkeypatch.setattr(result_module, "MAX_COMPRESSED_RECEIPT_BYTES", 8)
    monkeypatch.setattr(Path, "open", lambda self, mode="r": stream)
    with pytest.raises(ValueError, match="RESULT_RECEIPT_COMPRESSED_TOO_LARGE"):
        result_module.DurableResultStore._read_compressed(target)
    assert observed == [9]


def test_oversized_receipt_fails_closed_without_deleting_disk_evidence(tmp_path, monkeypatch):
    import pytest
    import easychange.core.result_store as result_module

    store = DurableResultStore(tmp_path / "results")
    sequence = "QCORRUPT1"
    store.mark_running(sequence, "a" * 64, ":job", mutation=True)
    target = store._path(sequence)
    before = target.read_bytes()
    assert len(before) > 8
    monkeypatch.setattr(result_module, "MAX_COMPRESSED_RECEIPT_BYTES", 8)
    with pytest.raises(RuntimeError, match="RESULT_RECEIPT_UNREADABLE"):
        store.get(sequence)
    assert target.read_bytes() == before
    with pytest.raises(RuntimeError, match="RESULT_RECEIPT_UNREADABLE"):
        store.mark_running(sequence, "a" * 64, ":job", mutation=True)


def test_active_result_store_record_survives_terminal_ttl_and_count_pressure(tmp_path, monkeypatch):
    import os
    import time

    root = tmp_path / "results"
    store = DurableResultStore(
        root,
        max_results=16,
        ttl_seconds=60,
        active_ttl_seconds=24 * 60 * 60,
    )
    active = "QACTIVE01"
    store.mark_running(active, "a" * 64, ":build-job", mutation=True)

    old = time.time() - 120
    active_path = root / f"{active}.json.zlib"
    os.utime(active_path, (old, old))

    for index in range(24):
        sequence = f"QTERM{index:04d}"
        store.mark_running(sequence, "b" * 64, ":state", mutation=False)
        store.complete(
            sequence,
            "b" * 64,
            ":state",
            {
                "ok": True,
                "command": "state",
                "data": {"index": index},
                "error": None,
                "code": None,
                "command_id": f"C{index}",
                "duration_ms": 1,
                "sequence": sequence,
            },
        )

    store._cleanup()

    retained = store.get(active)
    assert retained is not None
    assert retained["state"] == "RUNNING"
    assert retained["mutation"] is True


def test_active_result_store_record_expires_only_after_active_ttl(tmp_path, monkeypatch):
    import os
    import time

    root = tmp_path / "results"
    store = DurableResultStore(
        root,
        ttl_seconds=60,
        active_ttl_seconds=3600,
    )
    sequence = "QACTIVE02"
    store.mark_running(sequence, "c" * 64, ":test-job", mutation=False)
    target = root / f"{sequence}.json.zlib"

    old = time.time() - 3700
    os.utime(target, (old, old))
    value = store._decode(target.read_bytes())
    value["started_at"] = old
    value["created_at"] = old
    target.write_bytes(store._encode(value))
    os.utime(target, (old, old))

    assert store.get(sequence) is None
    assert not target.exists()
