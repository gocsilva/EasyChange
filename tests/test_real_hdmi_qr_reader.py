"""Production HDMI QR reader and safe unsequenced result handoff."""
from __future__ import annotations
import json
import hashlib
import sys
from types import SimpleNamespace
from easychange.remote.optical_protocol import encode_result_chunks
from mcp_adapter import optical_reader


def test_zxing_readable_physical_style_four_cell_grid(monkeypatch):
    """ZXing results must not be erased by OpenCV returning no QR cells."""
    packets = encode_result_chunks({
        "sequence": "-", "ok": True, "command": "read",
        "data": {"lines": ["28|", "29|value", "30|}", "31|"],
                 "large": "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(60))}
    }, chunk_chars=120)
    assert len(packets) >= 4
    four = packets[:4]
    zxing = SimpleNamespace(
        BarcodeFormat=SimpleNamespace(QRCode="QR"),
        read_barcodes=lambda *args, **kwargs: [
            SimpleNamespace(text=value) for value in four
        ],
    )
    monkeypatch.setitem(sys.modules, "zxingcpp", zxing)
    frame = SimpleNamespace(shape=(720, 1280, 3))
    assert optical_reader.decode_hdmi_qr(frame) == four


def test_unsequenced_completed_response_rotates_safely():
    rx = optical_reader.OpticalAssembler()
    one = encode_result_chunks({
        "sequence": "-", "ok": True, "command": "read",
        "data": {"lines": ["original"]}
    })
    two = encode_result_chunks({
        "sequence": "-", "ok": True, "command": "search",
        "data": {"matches": [{"path": "query.sql", "line": 4}]}
    })
    r1 = rx.ingest(one)
    assert r1["complete"] and r1["payload"]["command"] == "read"
    r2 = rx.ingest(two)
    assert r2["complete"] and r2["payload"]["command"] == "search"
    assert r2["payload"]["data"]["matches"][0]["path"] == "query.sql"


def test_immutable_explicit_sequence_cannot_switch_payload():
    rx=optical_reader.OpticalAssembler()
    old=encode_result_chunks({
        "sequence":"QIMMUTABLE","ok":True,"command":"read",
        "data":{"lines":["original"]}
    })
    changed=encode_result_chunks({
        "sequence":"QIMMUTABLE","ok":True,"command":"search",
        "data":{"matches":["untrusted-change"]}
    })
    first=rx.ingest(old)
    assert first["complete"]
    second=rx.ingest(changed)
    assert second["payload"]["command"]=="read"
    assert second["payload"]["data"]["lines"]==["original"]


def test_incomplete_unsequenced_result_is_not_discarded_by_competing_digest():
    rx=optical_reader.OpticalAssembler()
    old=encode_result_chunks({
        "sequence":"-","ok":True,"command":"read",
        "data":{"lines":[hashlib.sha256(str(i).encode()).hexdigest() for i in range(130)]}
    }, chunk_chars=120)
    new=encode_result_chunks({
        "sequence":"-","ok":True,"command":"search",
        "data":{"matches":[hashlib.sha256(f"new{i}".encode()).hexdigest() for i in range(130)]}
    }, chunk_chars=120)
    assert len(old)>1 and len(new)>1
    rx.ingest(old[:1])
    before=dict(rx.chunks)
    result=rx.ingest(new[:1])
    assert not result["complete"]
    assert rx.chunks==before
