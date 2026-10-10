"""Ambiguous execution receipts must never be replayed as fresh work."""
from __future__ import annotations

import json
import zlib

import pytest

from easychange.core.result_store import DurableResultStore


@pytest.mark.parametrize("raw,reason",(
    (b'{"state":"DONE","state":"RUNNING"}',"RESULT_RECEIPT_DUPLICATE_KEY"),
    (b'{"state":"DONE","result":{"ok":true,"ok":false}}',
     "RESULT_RECEIPT_DUPLICATE_KEY"),
    (b'{"state":"RUNNING","started_at":NaN}',
     "RESULT_RECEIPT_NONFINITE_NUMBER"),
    (b'{"state":"RUNNING","started_at":Infinity}',
     "RESULT_RECEIPT_NONFINITE_NUMBER"),
    (b'{"state":"RUNNING","started_at":1e10000}',
     "RESULT_RECEIPT_NONFINITE_NUMBER"),
))
def test_corrupt_receipt_cannot_be_misread_as_a_new_sequence(tmp_path, raw, reason):
    store=DurableResultStore(tmp_path/"store")
    sequence="QSTRICT001"
    path=store._path(sequence)
    wire=zlib.compress(raw)
    path.write_bytes(wire)
    with pytest.raises(RuntimeError,match="RESULT_RECEIPT_UNREADABLE"):
        store.get(sequence)
    # Never erase evidence or reissue work from an unreadable receipt.
    with pytest.raises(RuntimeError,match="RESULT_RECEIPT_UNREADABLE"):
        store.claim_running(sequence,"a"*64,":unsafe",mutation=True)
    assert path.read_bytes()==wire
    with pytest.raises(ValueError,match=reason):
        store._decode(wire)


def test_writer_refuses_nonstandard_nan_before_atomic_replacement(tmp_path):
    store=DurableResultStore(tmp_path/"store")
    sequence="QSTRICT002"
    data=store.mark_running(sequence,"a"*64,":safe",mutation=False)
    prior=store._path(sequence).read_bytes()
    data["result"]={"bad":float("nan")}
    with pytest.raises(ValueError):
        store._write(sequence,data,run_cleanup=False)
    assert store._path(sequence).read_bytes()==prior


def test_valid_finite_receipt_and_nested_values_still_round_trip(tmp_path):
    store=DurableResultStore(tmp_path/"store")
    original={"state":"DONE","finished_at":123.5,
              "result":{"count":3,"text":"áudio","nested":[{"ok":True}]}}
    restored=store._decode(store._encode(original))
    assert restored==original
    assert json.loads(json.dumps(restored))==restored
