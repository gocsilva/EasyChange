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



def test_incomplete_done_receipt_is_missing_result_not_running_or_reexecuted(tmp_path, monkeypatch):
    target=tmp_path / "sample.txt"
    target.write_text("alpha\\nbeta\\n".replace("\\n", "\n"),encoding="utf-8")
    service=CommandService(Workspace.open(tmp_path))
    try:
        operation={"op":"operation","type":"replace_exact",
                   "path":"sample.txt","old_text":"beta",
                   "new_text":"BETA","expected_occurrences":1}
        wire=":j1 "+json.dumps(operation,separators=(",",":"))
        first=service.execute(":ec QDONEMISSING1 "+wire)
        assert first.ok is True
        receipt=service.result_store.get("QDONEMISSING1")
        assert receipt["state"]=="DONE"
        receipt.pop("result",None)
        receipt["result_expired"]=False
        service.result_store._write("QDONEMISSING1",receipt,run_cleanup=False)
        def fail_if_reexecuted(*args,**kwargs):
            raise AssertionError("already executed mutation was replayed")
        monkeypatch.setattr(service,"_execute_structured",fail_if_reexecuted)
        again=service.execute(":ec QDONEMISSING1 "+wire)
        assert again.ok is False
        assert again.code=="SEQUENCE_RESULT_MISSING"
        assert again.data["state"]=="DONE"
        assert again.data["duplicate"] is True
        assert target.read_text(encoding="utf-8")=="alpha\\nBETA\\n".replace("\\n", "\n")
        assert service.result_store.get("QDONEMISSING1")["state"]=="DONE"
    finally:
        service.close()


def test_malformed_durable_done_result_never_reexecutes_mutation(tmp_path, monkeypatch):
    target=tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n",encoding="utf-8")
    service=CommandService(Workspace.open(tmp_path))
    try:
        operation={"op":"operation","type":"replace_exact",
                   "path":"sample.txt","old_text":"beta",
                   "new_text":"BETA","expected_occurrences":1}
        wire=":j1 "+json.dumps(operation,separators=(",",":"))
        first=service.execute(":ec QDONEMALFORMED1 "+wire)
        assert first.ok is True
        original=service.result_store.get("QDONEMALFORMED1")
        assert original["state"]=="DONE"
        def forbidden(*_args,**_kwargs):
            raise AssertionError("terminal sequence reexecuted")
        monkeypatch.setattr(service,"_execute_structured",forbidden)
        examples=(
            {"ok":True,"command":"operation","data":["not-a-dict"]},
            {"ok":"false","command":"operation","data":{}},
            {"ok":True,"command":"operation","data":{},"unknown_field":1},
            {"command":"operation","data":{}},
        )
        for broken in examples:
            receipt=dict(original)
            receipt["result"]=broken
            receipt["result_expired"]=False
            service.result_store._write(
                "QDONEMALFORMED1",receipt,run_cleanup=False)
            replay=service.execute(":ec QDONEMALFORMED1 "+wire)
            assert replay.ok is False
            assert replay.code=="SEQUENCE_RESULT_MISSING"
            assert replay.data["state"]=="DONE"
            assert target.read_text(encoding="utf-8")=="alpha\nBETA\n"
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


def test_cold_result_payload_streams_json_with_original_bytes_and_hash(tmp_path, monkeypatch):
    import hashlib
    import easychange.core.result_store as result_module

    store=DurableResultStore(tmp_path/"results")
    seq="QCOLDWIRE1"
    store.mark_running(seq,"a"*64,":read",mutation=False)
    result={"ok":True,"command":"read",
            "data":{"accent":"ação","emoji":"🙂","status":[1,False,None]}}
    terminal=store.complete(seq,"a"*64,":read",result)
    expected=json.dumps(result,ensure_ascii=False,allow_nan=False,
                        separators=(",",":")).encode("utf-8")
    store._payload_cache.clear()
    store._payload_cache_bytes=0
    with monkeypatch.context() as patcher:
        patcher.setattr(result_module.json,"dumps",
                        lambda *args,**kwargs: (_ for _ in ()).throw(
                            AssertionError("full json.dumps on cache miss")))
        payload=store._result_payload(seq)
    assert payload is not None
    value,raw,digest=payload
    assert value==result
    assert raw==expected
    assert digest==hashlib.sha256(expected).hexdigest()
    assert terminal["result_hash"]==digest
    assert terminal["result_bytes"]==len(raw)


def test_cold_result_encoder_rejects_large_json_before_full_materialization(monkeypatch):
    import pytest
    import easychange.core.result_store as result_module

    shared={"text":"🙂"*120}
    value={"data":[shared,shared,shared]}
    with monkeypatch.context() as patcher:
        patcher.setattr(result_module,"MAX_DECODED_RECEIPT_BYTES",72)
        patcher.setattr(result_module.json,"dumps",
                        lambda *args,**kwargs: (_ for _ in ()).throw(
                            AssertionError("unbounded json.dumps")))
        with pytest.raises(ValueError,match="RESULT_RECEIPT_DECOMPRESSED_TOO_LARGE"):
            DurableResultStore._encode_result_json(value)


def test_receipt_encoder_streams_byte_exact_json_without_full_dumps(monkeypatch):
    import zlib
    import easychange.core.result_store as result_module

    payload={"sequence":"QSTREAMJSON1","ok":True,
             "data":{"accent":"ação","emoji":"🙂","values":[1,2,False,None]}}
    expected=json.dumps(payload,ensure_ascii=False,allow_nan=False,
                        separators=(",",":")).encode("utf-8")
    with monkeypatch.context() as ctx:
        ctx.setattr(result_module.json,"dumps",
                    lambda *args,**kwargs: (_ for _ in ()).throw(
                        AssertionError("FULL_JSON_SERIALIZATION")))
        packed=DurableResultStore._encode(payload)
    assert zlib.decompress(packed)==expected


def test_receipt_writer_rejects_shared_dag_expansion_before_compression(monkeypatch):
    import pytest
    import easychange.core.result_store as result_module

    node={"result":"many shared references"}
    for _ in range(24):
        node=[node,node]
    with monkeypatch.context() as ctx:
        ctx.setattr(result_module,"MAX_DECODED_RECEIPT_BYTES",256)
        ctx.setattr(result_module.zlib,"compress",
                    lambda *args,**kwargs: (_ for _ in ()).throw(
                        AssertionError("COMPRESSED_OVERSIZED_JSON")))
        with pytest.raises(
                ValueError,match="RESULT_RECEIPT_DECOMPRESSED_TOO_LARGE"):
            DurableResultStore._encode({"payload":node})


def test_complete_limits_shared_dag_json_before_full_dumps_or_receipt_write(tmp_path, monkeypatch):
    import pytest
    import easychange.core.result_store as result_module

    store=DurableResultStore(tmp_path / "receipts")
    sequence="QCOMPLETEBUDGET1"
    digest="a"*64
    store.mark_running(sequence,digest,":read",mutation=False)
    target=store._path(sequence)
    original=target.read_bytes()
    shared={"text":"shared references"}
    for _ in range(20):
        shared=[shared,shared]
    with monkeypatch.context() as ctx:
        ctx.setattr(result_module,"MAX_DECODED_RECEIPT_BYTES",4096)
        ctx.setattr(result_module.json,"dumps",
                    lambda *args,**kwargs: (_ for _ in ()).throw(
                        AssertionError("FULL_RESULT_DUMPS")))
        with pytest.raises(
                ValueError,match="RESULT_RECEIPT_DECOMPRESSED_TOO_LARGE"):
            store.complete(sequence,digest,":read",
                           {"ok":True,"data":shared})
    assert target.read_bytes()==original
    assert store.get(sequence)["state"]=="RUNNING"


def test_cached_receipt_is_invalidated_when_replaced_by_symlink_to_same_inode(tmp_path):
    import pytest

    store = DurableResultStore(tmp_path / "receipts")
    sequence = "QCACHEDSYMLINK"
    result = {"ok": True, "command": "state", "sequence": sequence,
              "data": {"text": "unchanged"}}
    store.mark_running(sequence, "a" * 64, ":state", mutation=False)
    store.complete(sequence, "a" * 64, ":state", result)
    assert store.header(sequence)["success"] is True
    assert store.result(sequence) == result

    # Move the existing regular inode away, then make its original pathname
    # a symlink to that *same inode*. Following stat() would see an identical
    # stamp and mistakenly accept the old cached header/result.
    original = store._path(sequence)
    moved = tmp_path / "saved-valid-receipt.bin"
    original.replace(moved)
    try:
        original.symlink_to(moved)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("filesystem does not permit symlinks: " + type(exc).__name__)

    with pytest.raises(RuntimeError, match="RESULT_RECEIPT_UNREADABLE"):
        store.header(sequence)
    with pytest.raises(RuntimeError, match="RESULT_RECEIPT_UNREADABLE"):
        store.result(sequence)
    assert moved.is_file()
    assert original.is_symlink()


def test_symlinked_durable_receipt_cannot_read_unrelated_file_or_be_reexecuted(tmp_path):
    import pytest

    store = DurableResultStore(tmp_path / "receipts")
    sequence = "QSYMLINKSAFE"
    outside = tmp_path / "unrelated-secret.data"
    outside.write_bytes(b"PRIVATE_UNRELATED_CONTENT")
    receipt = store._path(sequence)
    try:
        receipt.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("filesystem does not permit symlink creation: " + type(exc).__name__)

    with pytest.raises(RuntimeError, match="RESULT_RECEIPT_UNREADABLE"):
        store.get(sequence)
    with pytest.raises(RuntimeError, match="RESULT_RECEIPT_UNREADABLE"):
        store.claim_running(sequence, "a" * 64, ":state", mutation=False)
    # Cleanup must not follow, decode, overwrite or delete this foreign file.
    store._cleanup()
    assert receipt.is_symlink()
    assert outside.read_bytes() == b"PRIVATE_UNRELATED_CONTENT"


def test_dangling_receipt_symlink_is_uncertain_not_an_unseen_sequence(tmp_path):
    import pytest

    store = DurableResultStore(tmp_path / "receipts")
    receipt = store._path("QSYMLINKDANGLING")
    try:
        receipt.symlink_to(tmp_path / "not-present")
    except (OSError, NotImplementedError) as exc:
        pytest.skip("filesystem does not permit symlink creation: " + type(exc).__name__)
    with pytest.raises(RuntimeError, match="RESULT_RECEIPT_UNREADABLE"):
        store.claim_running("QSYMLINKDANGLING", "a" * 64, ":state", mutation=False)
    assert receipt.is_symlink()


def test_durable_completion_requires_literal_true_for_success(tmp_path):
    store = DurableResultStore(tmp_path / "success-receipts")
    for position, marker in enumerate(("false", "true", 1, -1, [], {}, None, False, 0, True)):
        sequence = "QSTRICTOK" + str(position)
        with_hash = "a" * 64
        store.mark_running(sequence, with_hash, ":state", mutation=False)
        completed = store.complete(
            sequence, with_hash, ":state",
            {"ok": marker, "command": "state", "sequence": sequence},
        )
        expected = marker is True
        assert completed["success"] is expected
        assert store.status(sequence)["success"] is expected
        assert store.header(sequence)["success"] is expected
        assert store.result(sequence)["ok"] == marker

    # A duplicate delivery cannot turn an already terminal false outcome
    # into a success without acquiring a new sequence claim.
    unchanged = store.complete(
        "QSTRICTOK0", "a" * 64, ":state", {"ok": True, "command": "state"})
    assert unchanged["success"] is False


def test_complete_incremental_json_preserves_exact_result_hash_and_bytes(tmp_path):
    import hashlib

    store=DurableResultStore(tmp_path / "results")
    sequence="QCOMPLETEXACT1"
    digest="b"*64
    store.mark_running(sequence,digest,":read",mutation=False)
    result={"ok":True,"data":{"city":"São Paulo","emoji":"🙂",
                              "flags":[False,None,True],"values":[1,2,3]}}
    wire=json.dumps(result,ensure_ascii=False,separators=(",",":")).encode("utf-8")
    finished=store.complete(sequence,digest,":read",result)
    assert finished["result_hash"]==hashlib.sha256(wire).hexdigest()
    assert finished["result_bytes"]==len(wire)
    assert store.result(sequence)==result
    assert store.header(sequence)["sha256"]==finished["result_hash"]


def test_invalid_chunk_sizes_never_load_durable_result_or_raise_uncaught(tmp_path, monkeypatch):
    service=CommandService(Workspace.open(tmp_path))
    try:
        def forbidden(*args,**kwargs):
            raise AssertionError("INVALID_CHUNK_SIZE_DECODED_RESULT")

        for method in ("header","chunk_meta","chunk","chunks",
                       "stream_meta","stream_chunk","stream_chunks"):
            monkeypatch.setattr(service.result_store,method,forbidden)
        cases=(
            ("result_header",{}),
            ("result_meta",{}),
            ("result_chunk",{"chunk_index":0}),
            ("result_chunks",{"chunk_indexes":[0]}),
            ("result_stream_meta",{}),
            ("result_stream_chunk",{"chunk_index":0}),
            ("result_stream_chunks",{"chunk_indexes":[0]}),
        )
        for operation,extra in cases:
            for invalid in ("9"*100000,{},[],True,0.5,"bad"):
                reply=service._execute_structured({
                    "op":"operation","type":operation,
                    "sequence":"QSIZELIMIT01","chunk_bytes":invalid,
                    **extra,
                })
                assert reply.ok is False
                assert reply.code==(
                    "INVALID_RESULT_STREAM_CHUNK"
                    if "stream" in operation else "INVALID_RESULT_CHUNK")
    finally:
        service.close()


def test_valid_numeric_chunk_size_is_clamped_before_single_chunk_dispatch(tmp_path, monkeypatch):
    from unittest.mock import Mock

    service=CommandService(Workspace.open(tmp_path))
    try:
        observer=Mock(return_value={"chunk_index":0})
        monkeypatch.setattr(service.result_store,"chunk",observer)
        for raw,expected in (("1024",1024),("1",256),("9000",4096)):
            response=service._execute_structured({
                "op":"operation","type":"result_chunk",
                "sequence":"QVALIDSIZE01","chunk_index":"0",
                "chunk_bytes":raw,
            })
            assert response.ok is True
            assert observer.call_args.kwargs["chunk_bytes"]==expected
    finally:
        service.close()


def test_invalid_optical_recovery_indexes_never_decode_existing_receipt(tmp_path, monkeypatch):
    service=CommandService(Workspace.open(tmp_path))
    try:
        bad="9"*100000

        def forbidden(*args,**kwargs):
            raise AssertionError("INVALID_FRAME_INDEX_TRIGGERED_RESULT_DECODE")

        monkeypatch.setattr(service.result_store,"result",forbidden)
        for name,item in (
            ("optical_chunk",{"chunk_index":bad}),
            ("optical_chunks",{"chunk_indexes":[bad]}),
            ("optical_chunk",{"chunk_index":True}),
            ("optical_chunks",{"chunk_indexes":[0.9]}),
            ("optical_chunks",{"chunk_indexes":[]}),
        ):
            reply=service._execute_structured({
                "op":"operation","type":name,"sequence":"QSAFEOPTICAL01",**item})
            assert reply.ok is False
            assert reply.code=="INVALID_RESULT_CHUNK"
    finally:
        service.close()


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


def test_invalid_single_chunk_index_does_not_decompress_receipt_first(tmp_path, monkeypatch):
    import pytest

    store=DurableResultStore(tmp_path / "receipts")
    index="9"*100000

    def fail_expensive_read(*_args,**_kwargs):
        raise AssertionError("INVALID_INDEX_TRIGGERED_EXPENSIVE_DECOMPRESSION")

    monkeypatch.setattr(store,"_result_payload",fail_expensive_read)
    monkeypatch.setattr(store,"_result_stream_payload",fail_expensive_read)

    for invalid in (index,True,False,0.5,None,{},[]):
        with pytest.raises(ValueError,match="INVALID_RESULT_CHUNK_INDEX"):
            store.chunk("QSAFEINDEX1",invalid)
        with pytest.raises(ValueError,match="INVALID_RESULT_STREAM_CHUNK_INDEX"):
            store.stream_chunk("QSAFEINDEX1",invalid)
    with pytest.raises(ValueError,match="INVALID_RESULT_STREAM_CHUNK_INDEX"):
        store.stream_chunks("QSAFEINDEX1",[index])


def test_single_chunk_helpers_preserve_valid_numeric_string_index(tmp_path):
    store=DurableResultStore(tmp_path / "receipts")
    seq="QVALIDINDEX01"
    store.mark_running(seq,"a"*64,":state",mutation=False)
    store.complete(seq,"a"*64,":state,{
        "ok":True,"command":"state",
        "data":{"payload":"A"*40000},"sequence":seq,
    })
    assert store.chunk(seq,"0")["chunk_index"]==0
    assert store.stream_chunk(seq,"0")["chunk_index"]==0


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


def test_terminal_header_cache_isolates_nested_mutation_receipt(tmp_path):
    store=DurableResultStore(tmp_path / "results")
    sequence="QHEADERISOLATION01"
    source={"state":"VALIDATED","nested":{"review":"approved"}}
    store.mark_running(sequence,"a"*64,":patch",mutation=True)
    store.complete(
        sequence,"a"*64,":patch",
        {"ok":True,"command":"patch","data":{},"sequence":sequence},
        mutation_receipt=source)
    first=store.header(sequence)
    assert first["mutation_receipt"]["nested"]["review"]=="approved"
    first["mutation_receipt"]["nested"]["review"]="tampered by caller"
    source["nested"]["review"]="mutated after publication"
    next_header=store.header(sequence)
    assert next_header["mutation_receipt"]["nested"]["review"]=="approved"
    assert store.get(sequence)["mutation_receipt"]["nested"]["review"]=="approved"


def test_terminal_header_cache_skips_oversized_nested_receipt(tmp_path, monkeypatch):
    import easychange.core.result_store as store_module

    store=DurableResultStore(tmp_path / "results")
    sequence="QHEADEROVERBUDGET01"
    store.mark_running(sequence,"b"*64,":patch",mutation=True)
    # Test a small budget so it is obvious the cache does not retain a
    # large per-receipt payload even when the underlying receipt is valid.
    monkeypatch.setattr(store_module,"MAX_CACHED_TERMINAL_HEADER_BYTES",512)
    evidence="E"*4096
    store.complete(
        sequence,"b"*64,":patch",
        {"ok":True,"command":"patch","data":{},"sequence":sequence},
        mutation_receipt={"state":"VALIDATED","evidence":evidence})
    assert sequence not in store._terminal_header_cache
    first=store.header(sequence)
    assert first["mutation_receipt"]["evidence"]==evidence
    assert sequence not in store._terminal_header_cache
    # Omitting the fast cache may be slower but must never discard the
    # authoritative result or alter replay/dedup semantics.
    assert store.get(sequence)["mutation_receipt"]["evidence"]==evidence


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


def test_claim_lock_rejects_symlink_without_touching_foreign_file(tmp_path):
    import pytest

    store = DurableResultStore(tmp_path / "results")
    foreign = tmp_path / "not-a-lock.txt"
    foreign.write_bytes(b"must-stay-unchanged")
    lock_path = store._claim_lock_path
    try:
        lock_path.symlink_to(foreign)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not available with this account")
    with pytest.raises(RuntimeError, match="RESULT_CLAIM_UNSAFE_LOCK_PATH"):
        store.claim_running("QLOCK01", "a" * 64, ":write", mutation=True)
    assert foreign.read_bytes() == b"must-stay-unchanged"
    assert store.get("QLOCK01") is None
    lock_path.unlink()
    receipt, acquired = store.claim_running(
        "QLOCK01", "a" * 64, ":write", mutation=True)
    assert acquired is True
    assert receipt["state"] == "RUNNING"


def test_claim_lock_rejects_nonregular_fd_without_stranding_lock(tmp_path, monkeypatch):
    import pytest
    import easychange.core.result_store as result_store

    store = DurableResultStore(tmp_path / "results")
    with monkeypatch.context() as ctx:
        ctx.setattr(result_store.stat, "S_ISREG", lambda _mode: False)
        with pytest.raises(RuntimeError, match="RESULT_CLAIM_UNSAFE_LOCK_TYPE"):
            store.claim_running("QLOCK02", "a" * 64, ":write", mutation=True)
    record, acquired = store.claim_running(
        "QLOCK02", "a" * 64, ":write", mutation=True)
    assert acquired is True
    assert record["state"] == "RUNNING"


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


def test_oversized_decoded_result_keeps_original_claim_receipt(tmp_path, monkeypatch):
    import pytest
    import easychange.core.result_store as result_module

    store=DurableResultStore(tmp_path / "receipts")
    sequence="QWRITEBOUND1"
    digest="a"*64
    store.mark_running(sequence,digest,":read large",mutation=False)
    target=store._path(sequence)
    original=target.read_bytes()
    with monkeypatch.context() as ctx:
        ctx.setattr(result_module,"MAX_DECODED_RECEIPT_BYTES",2048)
        with pytest.raises(ValueError,match="RESULT_RECEIPT_DECOMPRESSED_TOO_LARGE"):
            store.complete(sequence,digest,":read large",
                           {"ok":True,"data":{"text":"x"*8000}})
        assert target.read_bytes()==original
        assert store.get(sequence)["state"]=="RUNNING"
        assert store.get(sequence)["result"] is None
    # A smaller result can still be stored and decoded normally.
    done=store.complete(sequence,digest,":read large",
                        {"ok":True,"data":{"text":"small"}})
    assert done["state"]=="DONE"
    assert store.get(sequence)["result"]["data"]["text"]=="small"


def test_oversized_compressed_result_never_overwrites_claim(tmp_path, monkeypatch):
    import hashlib
    import pytest
    import easychange.core.result_store as result_module

    store=DurableResultStore(tmp_path / "receipts")
    sequence="QWRITEBOUND2"
    digest="b"*64
    store.mark_running(sequence,digest,":read random",mutation=False)
    target=store._path(sequence)
    original=target.read_bytes()
    random_text="".join(hashlib.sha256(str(i).encode()).hexdigest()
                        for i in range(140))
    with monkeypatch.context() as ctx:
        ctx.setattr(result_module,"MAX_COMPRESSED_RECEIPT_BYTES",1024)
        with pytest.raises(ValueError,match="RESULT_RECEIPT_COMPRESSED_TOO_LARGE"):
            store.complete(sequence,digest,":read random",
                           {"ok":True,"data":{"text":random_text}})
        assert target.read_bytes()==original
        assert store.get(sequence)["state"]=="RUNNING"


def test_compressed_receipt_file_read_is_bounded(tmp_path, monkeypatch):
    import pytest
    import easychange.core.result_store as result_module

    target = tmp_path / "QBOUNDED.json.zlib"
    target.write_bytes(b"X" * 30)
    monkeypatch.setattr(result_module, "MAX_COMPRESSED_RECEIPT_BYTES", 8)
    with pytest.raises(ValueError, match="RESULT_RECEIPT_COMPRESSED_TOO_LARGE"):
        result_module.DurableResultStore._read_compressed(target)
    assert target.read_bytes() == b"X" * 30


def test_receipt_path_symlink_swap_fails_closed_at_open(tmp_path, monkeypatch):
    import os
    import pytest
    import easychange.core.result_store as result_module

    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("Platform lacks O_NOFOLLOW for atomic symlink protection")
    store=DurableResultStore(tmp_path / "results")
    sequence="QSYMLINKSWAP01"
    store.mark_running(sequence,"a"*64,":job",mutation=True)
    path=store._path(sequence)
    original=path.read_bytes()
    private=tmp_path / "private.txt"
    private.write_bytes(b"private content outside result store")
    real_open=os.open
    swapped=[]

    def replace_just_before_open(candidate, flags, *args, **kwargs):
        if str(candidate)==str(path) and not swapped:
            path.unlink()
            try:
                path.symlink_to(private)
            except (OSError,NotImplementedError):
                # The target is intentionally not recreated: fail closed.
                pass
            swapped.append(flags)
        return real_open(candidate,flags,*args,**kwargs)

    with monkeypatch.context() as ctx:
        ctx.setattr(result_module.os,"open",replace_just_before_open)
        with pytest.raises(RuntimeError,match="RESULT_RECEIPT_UNREADABLE"):
            store.get(sequence)
    assert len(swapped)==1 and swapped[0] & os.O_NOFOLLOW
    assert private.read_bytes()==b"private content outside result store"
    # The forged pathname is not a missing sequence. Restore the original
    # receipt and verify the same sequence remains in STARTED state.
    if path.is_symlink():
        path.unlink()
    path.write_bytes(original)
    assert store.get(sequence)["state"]=="RUNNING"


def test_failed_receipt_staging_wrapper_releases_raw_fd_and_preserves_claim(tmp_path, monkeypatch):
    import os
    import pytest
    import easychange.core.result_store as result_module

    store=DurableResultStore(tmp_path / "results")
    sequence="QWRAPPERFAIL01"
    store.mark_running(sequence, "a"*64, ":job", mutation=True)
    path=store._path(sequence)
    before=path.read_bytes()
    value=store.get(sequence)
    real_close=os.close
    closed=[]

    def record_close(fd):
        closed.append(fd)
        return real_close(fd)

    with monkeypatch.context() as ctx:
        ctx.setattr(result_module.os,"fdopen",
                    lambda *args,**kwargs: (_ for _ in ()).throw(
                        OSError("STAGING_WRAPPER_UNAVAILABLE")))
        ctx.setattr(result_module.os,"close",record_close)
        with pytest.raises(OSError,match="STAGING_WRAPPER_UNAVAILABLE"):
            store._write(sequence,value,run_cleanup=False)
    assert len(closed)==1
    assert path.read_bytes()==before
    assert not list(store.root.glob(path.name+".*.tmp"))
    assert store.get(sequence)["state"]=="RUNNING"


def test_failed_receipt_fd_wrapper_releases_raw_descriptor(tmp_path, monkeypatch):
    import os
    import pytest
    import easychange.core.result_store as result_module

    store=DurableResultStore(tmp_path / "results")
    sequence="QFDOPENFAIL01"
    store.mark_running(sequence,"a"*64,":job",mutation=True)
    before=store._path(sequence).read_bytes()
    real_close=os.close
    closed=[]

    def track_close(fd):
        closed.append(fd)
        return real_close(fd)

    def fail_fdopen(*args, **kwargs):
        raise OSError("SIMULATED_FDOPEN_FAILURE")

    with monkeypatch.context() as ctx:
        ctx.setattr(result_module.os,"fdopen",fail_fdopen)
        ctx.setattr(result_module.os,"close",track_close)
        with pytest.raises(RuntimeError,match="RESULT_RECEIPT_UNREADABLE"):
            store.get(sequence)
    assert len(closed)==1
    assert store._path(sequence).read_bytes()==before
    assert store.get(sequence)["state"]=="RUNNING"


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
