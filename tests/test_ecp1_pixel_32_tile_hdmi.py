"""Real 1280x720 HDMI-like 8x4 PIXEL grid, no mocked optical decoder."""
import json
import time

import pytest


@pytest.mark.parametrize("jpeg_quality", [None, 90, 75, 60])
def test_all_32_ecp1_packets_survive_720p_jpeg(jpeg_quality):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    try:
        from easychange.remote.pixel_transport import (
            SIDE, encode_pixel_bitmap, decode_pixel_frame)
    except ImportError:
        from app.easychange_pixel_transport import (
            SIDE, encode_pixel_bitmap, decode_pixel_frame)

    frame = np.full((720, 1280, 3), (18, 18, 18), dtype=np.uint8)
    expected = set()
    side_px, gap, top = 125, 3, 160
    left = (1280 - 8 * side_px - 7 * gap) // 2
    for slot in range(32):
        packet = {"p": "EC2", "s": "Q32CHECK", "i": slot, "n": 32,
                  "d": "x" * 110}
        payload = json.dumps(packet, separators=(",", ":"))
        expected.add(payload)
        small = np.frombuffer(encode_pixel_bitmap(payload),
                              dtype=np.uint8).reshape(SIDE, SIDE)
        tile = cv2.resize(small, (side_px, side_px),
                          interpolation=cv2.INTER_NEAREST)
        x = left + (slot % 8) * (side_px + gap)
        y = top + (slot // 8) * (side_px + gap)
        frame[y:y + side_px, x:x + side_px] = cv2.cvtColor(
            tile, cv2.COLOR_GRAY2BGR)

    if jpeg_quality is not None:
        ok, compressed = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
        assert ok
        frame = cv2.imdecode(compressed, cv2.IMREAD_COLOR)

    before = frame.copy()
    decoded = decode_pixel_frame(frame)
    assert set(decoded) == expected
    assert np.array_equal(frame, before), "MCP decoder must not mutate HDMI frame"
