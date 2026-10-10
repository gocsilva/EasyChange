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



def test_legacy_done_journal_without_result_cannot_reexecute_mutation(tmp_path, monkeypatch):
    import hashlib

    service=CommandService(Workspace.open(tmp_path))
    try:
        sequence="QLEGACYDONE1"
        operation={"op":"operation","type":"replace_exact",
                   "path":"missing.txt","old_text":"before","new_text":"after"}
        canonical=json.dumps(
            operation,ensure_ascii=False,sort_keys=True,separators=(",",":"))
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        service.state_store.data["ec_results"]={
            sequence:{"sha256":digest,"state":"DONE","mutation":True}}
        def forbidden(*args, **kwargs):
            raise AssertionError("unreplayable legacy mutation executed again")
        monkeypatch.setattr(service,"_execute_structured",forbidden)
        response=service.execute(":ec "+sequence+" :j1 "+json.dumps(operation))
        assert response.code=="SEQUENCE_RESULT_MISSING"
        assert service.result_store.get(sequence) is None
    finally:
        service.close()


def test_legacy_replay_does_not_overwrite_competing_durable_claim(tmp_path, monkeypatch):
    import hashlib
    from easychange.core.result import Result

    service=CommandService(Workspace.open(tmp_path))
    try:
        sequence="QLEGACYRACE1"
        operation={"op":"operation","type":"replace_exact",
                   "path":"missing.txt","old_text":"before","new_text":"after"}
        canonical=json.dumps(
            operation,ensure_ascii=False,sort_keys=True,separators=(",",":"))
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        prior_result=Result(True,"replace_exact",data={"replayed":True})
        service.state_store.data["ec_results"]={
            sequence:{"sha256":digest,"state":"DONE","mutation":True,
                      "result":prior_result.to_dict()}}
        original=service.result_store.claim_running

        def competing_claim(seq, command_hash, command, *, mutation):
            first, acquired=original(seq,command_hash,command,mutation=mutation)
            assert acquired
            return original(seq,command_hash,command,mutation=mutation)

        monkeypatch.setattr(service.result_store,"claim_running",competing_claim)
        monkeypatch.setattr(service,"_execute_structured",
                            lambda *a,**k: (_ for _ in ()).throw(
                                AssertionError("legacy mutation executed")))
        response=service.execute(":ec "+sequence+" :j1 "+json.dumps(operation))
        assert response.code=="RUNNING"
        assert service.result_store.get(sequence)["state"]=="RUNNING"
    finally:
        service.close()


def test_cleanup_retains_completed_mutation_claim_after_result_ttl(tmp_path, monkeypatch):
    import time

    root=tmp_path / "receipts"
    store=DurableResultStore(root, ttl_seconds=60)
    mutation="QMUTTOMB1"
    readonly="QREADOLD1"
    store.mark_running(mutation,"a"*64,":patch",mutation=True)
    store.complete(mutation,"a"*64,":patch",
                   {"ok":True,"command":"patch","data":{},"sequence":mutation},
                   mutation_receipt={"state":"COMMITTED",
                                     "lifecycle_state":"VALIDATED",
                                     "verified":True})
    store.mark_running(readonly,"b"*64,":state",mutation=False)
    store.complete(readonly,"b"*64,":state",
                   {"ok":True,"command":"state","data":{},"sequence":readonly})
    assert store.get(mutation)["state"]=="DONE"
    assert store.get(mutation)["mutation_state"]=="VALIDATED"
    # VALIDATED is a completed outcome, so the active-record guard alone
    # previously allowed its idempotency receipt to be deleted.
    assert not store._active_record(store.get(mutation))

    advanced=time.time()+61
    with monkeypatch.context() as local:
        local.setattr("easychange.core.result_store.time.time",lambda: advanced)
        store._cleanup()

    assert store._path(mutation).exists()
    assert store._path(readonly).exists()
    reopened=DurableResultStore(root,ttl_seconds=60)
    prior,acquired=reopened.claim_running(mutation,"a"*64,":patch",mutation=True)
    assert acquired is False
    assert prior["state"]=="DONE"
    assert isinstance(prior["result"],dict)
    # Non-mutating results may discard the large payload, but retain their
    # sequence identity so replay cannot silently turn into new execution.
    read_prior,read_acquired=reopened.claim_running(
        readonly,"b"*64,":state",mutation=False)
    assert read_acquired is False
    assert read_prior["state"]=="DONE"
    assert read_prior["result_expired"] is True
    assert read_prior["result"] is None


def test_optical_recovery_query_rejects_fractional_chunk_identifiers(tmp_path):
    service=CommandService(Workspace.open(tmp_path))
    try:
        seq="QOPTOINDEX"
        service.result_store.mark_running(seq,"a"*64,":state",mutation=False)
        service.result_store.complete(seq,"a"*64,":state",
            {"ok":True,"command":"state","data":{"message":"ok"},"sequence":seq})
        for kind in ("optical_chunk","optical_chunks"):
            for bad in (0.9,1.9,True,None):
                query={"op":"operation","type":kind,"sequence":seq}
                if kind=="optical_chunk":
                    query["chunk_index"]=bad
                else:
                    query["chunk_indexes"]=[bad]
                result=service._execute_structured(query)
                assert result.ok is False
                assert result.code=="INVALID_RESULT_CHUNK"
            query={"op":"operation","type":kind,"sequence":seq}
            if kind=="optical_chunk":
                query["chunk_index"]="0"
            else:
                query["chunk_indexes"]=["0"]
            result=service._execute_structured(query)
            assert result.ok is True
    finally:
        service.close()


def test_individual_recovery_chunks_do_not_coerce_bool_or_fractional_index(tmp_path):
    service=CommandService(Workspace.open(tmp_path))
    try:
        seq="QINDIVINDEX"
        service.result_store.mark_running(seq,"a"*64,":state",mutation=False)
        service.result_store.complete(seq,"a"*64,":state",{
            "ok":True,"command":"state",
            "data":{"text":"A"*40000},"sequence":seq,
        })
        for kind,code in (
            ("result_chunk","INVALID_RESULT_CHUNK"),
            ("result_stream_chunk","INVALID_RESULT_STREAM_CHUNK"),
        ):
            for bad in (True,False,0.1,1.9,None,"9"*100000):
                query={"op":"operation","type":kind,"sequence":seq,
                       "chunk_index":bad}
                reply=service._execute_structured(query)
                assert reply.ok is False
                assert reply.code==code
            valid=service._execute_structured({
                "op":"operation","type":kind,"sequence":seq,
                "chunk_index":"0",
            })
            assert valid.ok is True
    finally:
        service.close()


def test_chunk_batches_reject_fractional_indexes_but_allow_decimal_strings(tmp_path):
    import pytest

    store=DurableResultStore(tmp_path / "receipt", ttl_seconds=600)
    seq="QINDEXSAFE"
    store.mark_running(seq,"a"*64,":state",mutation=False)
    store.complete(seq,"a"*64,":state",
                   {"ok":True,"command":"state",
                    "data":{"payload":"A"*40000},"sequence":seq})
    assert store.chunks(seq,["0"])["chunks"][0]["chunk_index"]==0
    meta=store.stream_meta(seq)
    assert meta["eligible"] is True
    assert store.stream_chunks(seq,["0"])["chunks"][0]["chunk_index"]==0

    for rejected in (0.9,1.9,True,False,None,{},[]):
        with pytest.raises(ValueError,match="INVALID_RESULT_CHUNK_INDEX"):
            store.chunks(seq,[rejected])
        with pytest.raises(ValueError,match="INVALID_RESULT_STREAM_CHUNK_INDEX"):
            store.stream_chunks(seq,[rejected])


def test_stream_chunks_bounds_duplicate_request_and_preserves_retransmission(tmp_path):
    import pytest

    store=DurableResultStore(tmp_path / "receipts")
    sequence="QCHUNKBATCH01"
    store.mark_running(sequence, "a"*64, ":state", mutation=False)
    store.complete(sequence, "a"*64, ":state", {
        "ok":True,"command":"state",
        "data":{"payload":"A"*40000},"sequence":sequence,
    })
    assert store.stream_meta(sequence)["eligible"] is True

    # An accidental duplicate retransmission still yields one chunk.
    result=store.stream_chunks(sequence, ["0"]*256)
    assert result["returned_indexes"]==[0]
    assert len(result["chunks"])==1
    assert result["complete"] is True

    # Never iterate or deduplicate unbounded user-provided index lists.
    with pytest.raises(ValueError, match="RESULT_STREAM_CHUNK_REQUEST_TOO_LARGE"):
        store.stream_chunks(sequence, ["0"]*257)
    with pytest.raises(ValueError, match="RESULT_STREAM_CHUNK_BATCH_LIMIT"):
        store.stream_chunks(sequence, list(range(17)))


def test_logical_chunks_bounded_duplicates_preserve_retransmission(tmp_path):
    import pytest

    store=DurableResultStore(tmp_path / "receipts")
    seq="QLOGICALBATCH1"
    store.mark_running(seq, "a"*64, ":state", mutation=False)
    store.complete(seq, "a"*64, ":state", {
        "ok":True,"command":"state",
        "data":{"text":"A"*40000},"sequence":seq,
    })
    result=store.chunks(seq, ["0"]*256)
    assert result["returned_indexes"] == [0]
    assert len(result["chunks"]) == 1
    assert result["complete"] is True

    with pytest.raises(ValueError, match="RESULT_CHUNK_REQUEST_TOO_LARGE"):
        store.chunks(seq, ["0"]*257)
    with pytest.raises(ValueError, match="RESULT_CHUNK_BATCH_LIMIT"):
        store.chunks(seq, list(range(17)))


def test_oversize_decimal_index_is_rejected_without_integer_conversion(tmp_path):
    import pytest

    store=DurableResultStore(tmp_path / "receipts")
    seq="QLONGINDEX1"
    store.mark_running(seq, "a"*64, ":state", mutation=False)
    store.complete(seq, "a"*64, ":state", {
        "ok":True,"command":"state",
        "data":{"text":"A"*40000},"sequence":seq,
    })
    assert store.stream_meta(seq)["eligible"] is True

    # Python has a digit conversion threshold for very large decimal strings;
    # both transport paths must produce their own predictable input error.
    enormous="9"*100000
    with pytest.raises(ValueError, match="INVALID_RESULT_STREAM_CHUNK_INDEX"):
        store.stream_chunks(seq, [enormous])
    with pytest.raises(ValueError, match="INVALID_RESULT_CHUNK_INDEX"):
        store.chunks(seq, [enormous])


def test_stream_batch_uses_one_consistent_receipt_snapshot(tmp_path, monkeypatch):
    import base64
    import hashlib
    import zlib

    store=DurableResultStore(tmp_path / "receipts")
    seq="QSNAPSHOT01"

    def payload(text):
        raw=json.dumps({"ok":True,"data":{"text":text}},
                       separators=(",",":")).encode("utf-8")
        packed=zlib.compress(raw, level=1)
        return (packed, hashlib.sha256(packed).hexdigest(),
                hashlib.sha256(raw).hexdigest(),len(raw))

    initial=payload("A"*40000)
    replacement=payload("B"*40000)
    calls=[]
    def concurrent_snapshot(_sequence):
        calls.append(_sequence)
        return initial if len(calls)==1 else replacement

    monkeypatch.setattr(store, "_result_stream_payload", concurrent_snapshot)
    response=store.stream_chunks(seq,[0])
    assert len(calls)==1
    assert response["eligible"] is True
    assert response["stream_hash"]==initial[1]
    assert response["result_hash"]==initial[2]
    assert response["stream_bytes"]==len(initial[0])
    chunk=response["chunks"][0]
    assert chunk["stream_hash"]==response["stream_hash"]
    assert chunk["result_hash"]==response["result_hash"]
    encoded=chunk["payload_b64"]
    assert base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4))==initial[0]

    calls.clear()
    metadata=store.stream_meta(seq)
    assert metadata["stream_hash"]==initial[1]
    assert len(calls)==1


def test_terminal_header_cache_preserves_expired_replay_tombstone(tmp_path, monkeypatch):
    import time

    store=DurableResultStore(tmp_path / "receipts", ttl_seconds=60)
    sequence="QEXPIRE01"
    store.mark_running(sequence, "a"*64, ":state", mutation=False)
    store.complete(sequence, "a"*64, ":state",
                   {"ok":True,"command":"state","data":{},"sequence":sequence})
    recorded=store.header(sequence)
    assert recorded["finished"] is True
    target=store._path(sequence)
    assert target.exists()
    # A completed sequence remains replayable even after the result TTL.
    # Repeated header polls must not repeatedly decode unchanged tombstones.
    advance=time.time()+61
    with monkeypatch.context() as local:
        local.setattr("easychange.core.result_store.time.time",lambda:advance)
        local.setattr(store,"_decode",
                      lambda _raw: (_ for _ in ()).throw(
                          AssertionError("unnecessary receipt decode")))
        assert store.header(sequence)["sha256"]==recorded["sha256"]
    assert target.exists()
    assert store.get(sequence)["state"]=="DONE"


def test_cleanup_compacts_expired_read_result_but_keeps_replay_tombstone(tmp_path):
    import hashlib
    import os
    import time

    folder=tmp_path / "result-receipts"
    store=DurableResultStore(folder)
    sequence="QEXPIRED01"
    content="".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(120))
    store.mark_running(sequence,"a"*64,":state",mutation=False)
    original=store.complete(
        sequence,"a"*64,":state",
        {"ok":True,"command":"state","data":{"text":content},
         "sequence":sequence})
    receipt_path=store._path(sequence)
    original_size=receipt_path.stat().st_size
    old=time.time()-store.ttl_seconds-120
    os.utime(receipt_path,(old,old))
    store._cleanup()
    assert receipt_path.is_file()
    assert receipt_path.stat().st_size<original_size
    recovered=DurableResultStore(folder).get(sequence)
    assert recovered["state"]=="DONE"
    assert recovered["command_hash"]=="a"*64
    assert recovered["result_expired"] is True
    assert recovered["result"] is None
    assert recovered["expired_result_hash"]==original["result_hash"]
    assert store.status(sequence)["result_expired"] is True
    header=store.header(sequence)
    assert header["finished"] is True
    assert header["result_expired"] is True
    assert header["expired_result_hash"]==original["result_hash"]
    assert header["payload_length"]==0
    # Another maintenance cycle may not delete or re-expand the tombstone.
    store._cleanup()
    assert store.get(sequence)["result_expired"] is True


def test_expired_read_receipt_blocks_same_sequence_execution(tmp_path, monkeypatch):
    import os
    import time

    service=CommandService(Workspace.open(tmp_path))
    try:
        seq="QEXPIRED02"
        first=service.execute(":ec "+seq+" :state")
        assert first.sequence==seq
        target=service.result_store._path(seq)
        old=time.time()-service.result_store.ttl_seconds-120
        os.utime(target,(old,old))
        service.result_store._cleanup()
        def forbidden(*args,**kwargs):
            raise AssertionError("expired command executed a second time")
        monkeypatch.setattr(service,"execute_tokens",forbidden)
        second=service.execute(":ec "+seq+" :state")
        assert second.code=="SEQUENCE_RESULT_EXPIRED"
        assert second.data["duplicate"] is True
        assert service.result_store.get(seq)["state"]=="DONE"
    finally:
        service.close()


def test_header_cache_refreshes_after_other_process_replaces_receipt(tmp_path, monkeypatch):
    root=tmp_path / "results"
    first=DurableResultStore(root)
    second=DurableResultStore(root)
    seq="QHEADEREXT1"
    receipt={"state":"COMMITTED","lifecycle_state":"APPLIED",
             "mutation_id":"M-EXT","verified":True}
    first.mark_running(seq, "a"*64, ":patch", mutation=True)
    first.complete(
        seq, "a"*64, ":patch",
        {"ok":True,"command":"patch","data":{},"sequence":seq},
        mutation_receipt=receipt)
    cached=first.header(seq)
    assert cached["mutation_state"]=="APPLIED"

    def should_not_decode(_):
        raise AssertionError("unchanged cached header should avoid decoding")
    with monkeypatch.context() as ctx:
        ctx.setattr(first, "_decode", should_not_decode)
        assert first.header(seq)["mutation_state"]=="APPLIED"

    updated={"state":"ROLLED_BACK","lifecycle_state":"ROLLED_BACK",
             "mutation_id":"M-EXT","verified":True}
    second.update_mutation_state(seq,"ROLLED_BACK",mutation_receipt=updated)
    fresh=first.header(seq)
    assert fresh["mutation_state"]=="ROLLED_BACK"
    assert fresh["mutation_receipt"]["state"]=="ROLLED_BACK"
    assert fresh["sha256"] == cached["sha256"]

    second._path(seq).unlink()
    assert first.header(seq) is None


def test_payload_and_stream_caches_invalidate_after_other_process_compacts_receipt(tmp_path):
    import os
    import time

    root=tmp_path/"receipts"
    original=DurableResultStore(root)
    maintenance=DurableResultStore(root)
    sequence="QEXTERNALCOMPACT1"
    original.mark_running(sequence,"a"*64,":state",mutation=False)
    original.complete(
        sequence,"a"*64,":state",
        {"ok":True,"command":"state","data":{"text":"cache me"*1600},
         "sequence":sequence})
    assert original.result(sequence)["data"]["text"]
    assert original.stream_meta(sequence) is not None
    assert sequence in original._payload_cache
    assert sequence in original._stream_cache

    path=original._path(sequence)
    old=time.time()-maintenance.ttl_seconds-120
    os.utime(path,(old,old))
    maintenance._cleanup()
    assert maintenance.get(sequence)["result_expired"] is True
    assert path.exists()

    # The physical receipt is still present, but its atomic replacement
    # must invalidate both caches, including the compressed stream cache.
    assert original.stream_meta(sequence) is None
    assert original.result(sequence) is None
    assert sequence not in original._stream_cache
    assert sequence not in original._payload_cache
    assert original.header(sequence)["result_expired"] is True


def test_payload_cache_survives_unchanged_receipt_without_redecode(tmp_path, monkeypatch):
    original=DurableResultStore(tmp_path/"receipts")
    sequence="QUNMODIFIED1"
    original.mark_running(sequence,"a"*64,":state",mutation=False)
    original.complete(sequence,"a"*64,":state",
                      {"ok":True,"data":{"text":"keep"},"sequence":sequence})
    assert original.result(sequence)["data"]["text"]=="keep"
    with monkeypatch.context() as context:
        context.setattr(original,"_decode",
                        lambda data: (_ for _ in ()).throw(
                            AssertionError("unchanged cache decoded receipt")))
        assert original.result(sequence)["data"]["text"]=="keep"
        assert original.stream_meta(sequence) is not None


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


def test_active_result_store_record_remains_claimed_even_after_active_ttl(tmp_path, monkeypatch):
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

    persisted=store.get(sequence)
    assert persisted is not None
    assert persisted["state"]=="RUNNING"
    assert target.exists()
    replay,acquired=store.claim_running(
        sequence,"c"*64,":test-job",mutation=False)
    assert acquired is False
    assert replay["execution_id"]==persisted["execution_id"]
