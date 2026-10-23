"""Regression: forced PIXEL packets must actually fit the ECP1 bitmap.

The existing AUTO/QR adaptive fragment size is intentionally untouched.
"""
import json
from types import SimpleNamespace

from easychange.remote.optical_protocol import encode_result_bundle, decode_result_chunks
from easychange.remote.pixel_transport import encode_pixel_bitmap, decode_pixel_bitmap
from easychange.ui.main_window import MainWindow


def _chunks(**overrides):
    data = {"state": "READY", "workspace": r"C:\sources\EasyPay\bs2.core2.pix.cambio",
            "workspace_id": "W1", "instance_id": "A2F7D86",
            "file": ".codex_mcp_large_probe_20261022.txt",
            "line": 1, "transaction": "NONE", "index": "READY", "machine_mode": True,
            "focus": "COMMAND"}
    data.update(overrides)
    result = {"ok": True, "command": "state", "sequence": "Q1A166B38C861", "data": data}
    return result


def test_pixel_chunk_size_only_applies_to_pixel_display():
    assert MainWindow._pixel_chunk_chars(SimpleNamespace(remote_transport_mode="PIXEL")) == 130
    assert MainWindow._pixel_chunk_chars(SimpleNamespace(
        remote_transport_mode="AUTO", _host_optical_preference="PIXEL")) == 130
    for mode in ("AUTO", "QR", "GATT"):
        assert MainWindow._pixel_chunk_chars(SimpleNamespace(remote_transport_mode=mode)) is None


def test_realistic_state_is_all_pixel_not_qr_fallback():
    result = _chunks()
    packets, header = encode_result_bundle(result, chunk_chars=130, max_packets=31)
    assert len(packets) >= 2
    for payload in [header, *packets]:
        obj = json.loads(payload)
        obj["tm"] = "PIXEL"
        obj["rm"] = "PIXEL"
        visual_wire = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        # Reproduces MainWindow._build_optical_pixmap ECP1 capacity check.
        assert decode_pixel_bitmap(encode_pixel_bitmap(visual_wire)) == visual_wire
    restored = decode_result_chunks(packets)
    assert restored["sequence"] == result["sequence"]
    assert restored["data"]["state"] == "READY"


def test_default_qr_output_preserves_original_adaptive_chunking():
    result = _chunks()
    assert encode_result_bundle(result, chunk_chars=None) == encode_result_bundle(result)


def test_large_result_pixel_first_page_remains_crc_verified():
    result = _chunks(lines=[f"ROW-{i:04d}:" + str(i*i) for i in range(400)])
    packets, header = encode_result_bundle(
        result, chunk_chars=130, max_packets=31, overflow_packets=3)
    assert len(packets) <= 31
    assert json.loads(header)["w"] == len(packets)
    for packet in packets:
        metadata = json.loads(packet)
        metadata["tm"] = "PIXEL"
        metadata["rm"] = "PIXEL"
        wire = json.dumps(metadata, separators=(",", ":"))
        assert decode_pixel_bitmap(encode_pixel_bitmap(wire)) == wire
