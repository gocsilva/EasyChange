"""Compact EC2 packet density and page-count regression tests."""
import json
import math

import pytest
from easychange.remote.optical_protocol import adaptive_chunk_chars


def test_small_payload_chunks_keep_four_slot_compact_page():
    for token_chars in (100, 400, 600, 800):
        limit = adaptive_chunk_chars(token_chars)
        assert limit == 200
        assert math.ceil(token_chars / limit) <= 4

    # A larger payload keeps the existing bounded broadcast behavior.
    assert adaptive_chunk_chars(801) == 260
    assert adaptive_chunk_chars(1200) == 260
    assert adaptive_chunk_chars(3000) == 220
    assert adaptive_chunk_chars(8000) == 180


def test_small_payload_qr_modules_are_less_dense():
    qrcode = pytest.importorskip("qrcode")

    def qr_side(chars):
        packet = {
            "p": "EC2", "v": 2, "s": "QLEGIBILITY1", "i": 0, "n": 4,
            "h": "0" * 16, "c": "0" * 8, "e": "z",
            "d": "a" * chars,
        }
        code = qrcode.QRCode(
            box_size=1, border=4,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
        )
        code.add_data(json.dumps(packet, separators=(",", ":")))
        code.make(fit=True)
        return len(code.get_matrix())

    assert qr_side(200) < qr_side(260)
