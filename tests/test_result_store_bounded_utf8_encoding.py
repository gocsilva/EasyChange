"""Cold result streaming must bound temporary UTF-8 allocations."""
from __future__ import annotations

import json

import pytest

import easychange.core.result_store as module
from easychange.core.result_store import DurableResultStore


def test_cold_result_encoder_slices_large_encoder_fragments_before_utf8(monkeypatch):
    expected=json.dumps({"message":"🙂"*5000},ensure_ascii=False,
                        allow_nan=False,separators=(",",":"))

    class GuardedFragment(str):
        def encode(self,*args,**kwargs):
            raise AssertionError("FULL_FRAGMENT_UTF8_ALLOCATION")

    def encoded_fragments(self,_value):
        yield GuardedFragment(expected)

    with monkeypatch.context() as p:
        p.setattr(module.json.JSONEncoder,"iterencode",encoded_fragments)
        raw=DurableResultStore._encode_result_json({"message":"unused"})
    assert raw==expected.encode("utf-8")


def test_cold_result_budget_counts_utf8_bytes_not_characters(monkeypatch):
    payload={"data":"ação🙂"*30}
    expected=json.dumps(payload,ensure_ascii=False,allow_nan=False,
                        separators=(",",":")).encode("utf-8")
    with monkeypatch.context() as p:
        p.setattr(module,"MAX_DECODED_RECEIPT_BYTES",len(expected)-1)
        with pytest.raises(ValueError,
                           match="RESULT_RECEIPT_DECOMPRESSED_TOO_LARGE"):
            DurableResultStore._encode_result_json(payload)
    with monkeypatch.context() as p:
        p.setattr(module,"MAX_DECODED_RECEIPT_BYTES",len(expected))
        assert DurableResultStore._encode_result_json(payload)==expected
