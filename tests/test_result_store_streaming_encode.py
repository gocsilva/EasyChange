"""Durable receipts stream JSON to zlib without a second decoded-size buffer."""
from __future__ import annotations

import hashlib
import json
import zlib

import pytest

import easychange.core.result_store as receipt_module
from easychange.core.result_store import DurableResultStore


def test_streaming_receipt_preserves_json_wire_and_decode_contract(monkeypatch):
    value={"state":"DONE","sequence":"QSTREAMING1",
           "unicode":"é｜汉字😊","nested":{"events":[{"id":i} for i in range(30)]}}
    # Former code compressed an entire bytearray using zlib.compress.
    # Any regression to that expensive extra buffer must be observable.
    with monkeypatch.context() as context:
        context.setattr(receipt_module.zlib,"compress",
                        lambda *_a,**_k: (_ for _ in ()).throw(
                            AssertionError("full-size JSON buffer compressed")))
        encoded=DurableResultStore._encode(value)
    expected=json.dumps(value,ensure_ascii=False,
                        separators=(",",":"),allow_nan=False).encode("utf-8")
    assert zlib.decompress(encoded)==expected
    assert DurableResultStore._decode(encoded)==value


def test_streaming_encoder_rejects_decoded_size_while_json_expands(monkeypatch):
    monkeypatch.setattr(receipt_module,"MAX_DECODED_RECEIPT_BYTES",96)
    huge={"result":{"unicode":"á" * 70}}
    with pytest.raises(ValueError,match="RESULT_RECEIPT_DECOMPRESSED_TOO_LARGE"):
        DurableResultStore._encode(huge)


def test_streaming_encoder_rejects_compressed_size_without_full_json(monkeypatch):
    # Deterministic low-compressibility bytes expressed as ASCII digests.
    text="".join(hashlib.sha256(str(i).encode()).hexdigest()
                 for i in range(100))
    monkeypatch.setattr(receipt_module,"MAX_COMPRESSED_RECEIPT_BYTES",64)
    with pytest.raises(ValueError,match="RESULT_RECEIPT_COMPRESSED_TOO_LARGE"):
        DurableResultStore._encode({"payload":text})


def test_streaming_encoder_accepts_small_record_under_small_limits(monkeypatch):
    monkeypatch.setattr(receipt_module,"MAX_DECODED_RECEIPT_BYTES",128)
    monkeypatch.setattr(receipt_module,"MAX_COMPRESSED_RECEIPT_BYTES",128)
    value={"ok":True,"message":"small"}
    encoded=DurableResultStore._encode(value)
    assert DurableResultStore._decode(encoded)==value
