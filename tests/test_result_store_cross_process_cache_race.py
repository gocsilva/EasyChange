"""Receipt caches must never stamp obsolete bytes as a newer disk generation."""
from __future__ import annotations

import hashlib
import json
import zlib

from easychange.core.result_store import DurableResultStore


def _prepare(store, sequence, text):
    store.mark_running(sequence, "a"*64, ":state", mutation=False)
    store.complete(sequence, "a"*64, ":state", {
        "ok": True, "command": "state", "sequence": sequence,
        "data": {"text": text},
    })


def _replace_result(writer, sequence, text):
    # Simulate a legitimate second EasyChange instance atomically replacing a
    # receipt while the first instance is about to update an in-memory cache.
    with writer._claim_lock():
        value = writer.get(sequence)
        result = dict(value["result"])
        result["data"] = {"text": text}
        raw = json.dumps(result,ensure_ascii=False,separators=(",",":")).encode("utf-8")
        value["result"] = result
        value["result_hash"] = hashlib.sha256(raw).hexdigest()
        value["result_bytes"] = len(raw)
        writer._write(sequence,value,run_cleanup=False)


def test_payload_cache_does_not_pin_old_result_to_new_receipt(tmp_path,monkeypatch):
    folder=tmp_path/"receipts"
    reader=DurableResultStore(folder)
    writer=DurableResultStore(folder)
    sequence="QREPLACECACHE1"
    _prepare(reader,sequence,"old-data")
    reader._payload_cache.clear()
    reader._payload_cache_bytes=0

    original=reader.result
    changed=[False]
    def replace_between_read_and_stamp(seq):
        value=original(seq)
        if not changed[0]:
            changed[0]=True
            _replace_result(writer,sequence,"new-data")
        return value

    monkeypatch.setattr(reader,"result",replace_between_read_and_stamp)
    _value,raw,digest=reader._result_payload(sequence)
    assert changed==[True]
    assert json.loads(raw)["data"]["text"]=="new-data"
    assert digest==hashlib.sha256(raw).hexdigest()
    cached=reader._payload_cache_get(sequence)
    assert cached is not None and cached[0]==raw
    assert cached[1]==digest


def test_stream_cache_retries_when_another_instance_replaces_receipt(tmp_path,monkeypatch):
    folder=tmp_path/"receipts"
    reader=DurableResultStore(folder)
    writer=DurableResultStore(folder)
    sequence="QREPLACESTREAM1"
    _prepare(reader,sequence,"A"*40000)

    previous=reader._result_payload
    changed=[False]
    def replace_after_payload(seq):
        value=previous(seq)
        if not changed[0]:
            changed[0]=True
            _replace_result(writer,sequence,"B"*40000)
        return value

    monkeypatch.setattr(reader,"_result_payload",replace_after_payload)
    stream,stream_hash,result_hash,result_bytes=reader._result_stream_payload(sequence)
    assert changed==[True]
    unpacked=zlib.decompress(stream)
    assert json.loads(unpacked)["data"]["text"]=="B"*40000
    assert stream_hash==hashlib.sha256(stream).hexdigest()
    assert result_hash==hashlib.sha256(unpacked).hexdigest()
    assert result_bytes==len(unpacked)
    cached=reader._stream_cache_get(sequence)
    assert cached is not None and cached[0]==stream
