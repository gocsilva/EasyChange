"""ECP1: prioritize expanded registration crops on light backgrounds."""
import json
import time

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

import easychange.remote.pixel_transport as pixel


@pytest.mark.parametrize("background", [18, 255])
def test_32_hdmi_tiles_decode_with_bounded_candidate_trials(monkeypatch, background):
    frame = np.full((720, 1280, 3), background, dtype=np.uint8)
    expected = set()
    for index in range(32):
        wire = json.dumps(
            {"p": "EC2", "s": "QPIXELFAST", "i": index, "n": 32,
             "d": "X" * 80}, separators=(",", ":")
        )
        expected.add(wire)
        raw = np.frombuffer(pixel.encode_pixel_bitmap(wire), dtype=np.uint8)
        tile = cv2.resize(
            raw.reshape(pixel.SIDE, pixel.SIDE), (125, 125),
            interpolation=cv2.INTER_NEAREST,
        )
        x = 75 + (index % 8) * 145
        y = 55 + (index // 8) * 144
        frame[y:y+125, x:x+125, :] = tile[:, :, None]
    calls = [0]
    real_decode = pixel.decode_pixel_bitmap

    def observed_decode(bitmap, *, side=pixel.SIDE):
        calls[0] += 1
        return real_decode(bitmap, side=side)

    monkeypatch.setattr(pixel, "decode_pixel_bitmap", observed_decode)
    assert set(pixel.decode_pixel_frame(frame)) == expected
    # The previous light-background path used >=7 attempts per tile
    # (224 calls) before the expanded registration crop succeeded.
    assert calls[0] <= 96
