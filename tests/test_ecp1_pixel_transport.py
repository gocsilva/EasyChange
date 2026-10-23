"""ECP1 HDMI pixel transport regression tests.

The existing EC2 packet and mutation contracts are deliberately unchanged.
"""
import binascii
import json

import pytest

try:
    from easychange.remote.pixel_transport import (
        SIDE, MAX_PAYLOAD, encode_pixel_bitmap, decode_pixel_bitmap,
        decode_pixel_frame,
    )
except ImportError:
    from app.easychange_pixel_transport import (
        SIDE, MAX_PAYLOAD, encode_pixel_bitmap, decode_pixel_bitmap,
        decode_pixel_frame,
    )


@pytest.mark.parametrize("payload", [
    '{"p":"EC2H","v":1,"s":"QTEST1234","n":0,"w":0}',
    '{"p":"EC2","s":"QTEST1234","i":0,"n":1,"d":"YWJj","x":"352441c2"}',
    '{"p":"EC2","s":"QUNICODE","i":0,"n":1,"d":"'+("x"*150)+'"}',
])
def test_ecp1_roundtrip(payload):
    image = encode_pixel_bitmap(payload)
    assert len(image) == SIDE * SIDE
    assert decode_pixel_bitmap(image) == payload


def test_capacity_and_corruption_are_fail_closed():
    with pytest.raises(ValueError, match="CAPACITY"):
        encode_pixel_bitmap("x" * (MAX_PAYLOAD + 1))
    with pytest.raises(ValueError, match="CAPACITY"):
        encode_pixel_bitmap("")
    image = bytearray(encode_pixel_bitmap('{"p":"EC2H","v":1}'))
    # Flip the first bit of the *payload*, past the 10-byte frame header.
    image[(3 + (10 * 8) // 50) * SIDE + 3 + (10 * 8) % 50] ^= 255
    assert decode_pixel_bitmap(image) is None
    assert decode_pixel_bitmap(bytes(SIDE * SIDE)) is None
    assert decode_pixel_bitmap(b"") is None


def test_frame_decodes_two_independent_cells():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    frame = np.full((720, 1280, 3), (16, 24, 32), dtype=np.uint8)
    one = '{"p":"EC2H","v":1,"s":"QFRAME1234","n":2,"w":2}'
    two = '{"p":"EC2","s":"QFRAME1234","i":0,"n":2,"d":"YWJj"}'
    for text, x, y in ((one, 700, 260), (two, 920, 260)):
        small = np.frombuffer(encode_pixel_bitmap(text), dtype=np.uint8).reshape(SIDE, SIDE)
        tile = cv2.resize(small, (210, 210), interpolation=cv2.INTER_NEAREST)
        frame[y:y + 210, x:x + 210] = cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR)
    observed = decode_pixel_frame(frame)
    assert one in observed and two in observed


def test_blank_frame_contains_no_data():
    np = pytest.importorskip("numpy")
    assert decode_pixel_frame(np.zeros((720, 1280, 3), dtype=np.uint8)) == []


def test_compact_72_cell_capacity_crc_and_legacy_56_unchanged():
    from importlib import import_module
    mod = import_module(encode_pixel_bitmap.__module__)
    payload = json.dumps({"p": "EC2", "s": "QCOMPACT72",
                          "d": "x" * 320}, separators=(",", ":"))
    assert MAX_PAYLOAD < len(payload.encode()) <= mod.LARGE_MAX_PAYLOAD
    with pytest.raises(ValueError, match="CAPACITY"):
        encode_pixel_bitmap(payload)
    expanded = encode_pixel_bitmap(payload, side=mod.LARGE_SIDE)
    assert len(expanded) == mod.LARGE_SIDE ** 2
    assert decode_pixel_bitmap(expanded, side=mod.LARGE_SIDE) == payload
    assert decode_pixel_bitmap(expanded) is None
    damaged = bytearray(expanded)
    damaged[(3 + (10 * 8) // 66) * 72 + 3 + (10 * 8) % 66] ^= 255
    assert decode_pixel_bitmap(damaged, side=mod.LARGE_SIDE) is None
    assert decode_pixel_bitmap(encode_pixel_bitmap("legacy")) == "legacy"


@pytest.mark.parametrize("jpeg_quality", [None, 85])
def test_compact_72_cell_mjpeg_frame(jpeg_quality):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from importlib import import_module
    mod = import_module(encode_pixel_bitmap.__module__)
    payload = '{"p":"EC2","s":"QPIXEL72","d":"' + ("z" * 320) + '"}'
    frame = np.full((720, 1280, 3), (14, 17, 24), dtype=np.uint8)
    bitmap = np.frombuffer(encode_pixel_bitmap(payload, side=mod.LARGE_SIDE),
                           dtype=np.uint8).reshape(mod.LARGE_SIDE, mod.LARGE_SIDE)
    tile = cv2.resize(bitmap, (210, 210), interpolation=cv2.INTER_NEAREST)
    frame[430:640, 430:640] = cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR)
    if jpeg_quality is not None:
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
        assert ok
        frame = cv2.imdecode(jpg, cv2.IMREAD_COLOR)
    assert payload in decode_pixel_frame(frame)
