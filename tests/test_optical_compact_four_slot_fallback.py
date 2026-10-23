from __future__ import annotations

import sys
from types import SimpleNamespace
from mcp_adapter import optical_reader


class _Frame:
    shape = (1080, 1920, 3)

    def __getitem__(self, item):
        return SimpleNamespace(left=item[1].start)


class _Detector:
    def __init__(self, multi):
        self.multi = multi
        self.crop_calls = []

    def detectAndDecodeMulti(self, frame):
        return True, self.multi, None, None

    def detectAndDecode(self, frame):
        if not hasattr(frame, "left"):
            return "", None, None
        self.crop_calls.append(frame.left)
        return ("qr-fourth" if frame.left > 1500 else "qr-first"), None, None


def test_compact_footer_recovers_fourth_qr_not_found_by_multi(monkeypatch):
    detector = _Detector(["qr-first", "qr-second", "qr-third"])
    monkeypatch.setattr(optical_reader._qr_detectors, "detector", detector, raising=False)
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(error=RuntimeError))
    assert optical_reader.decode_hdmi_qr(_Frame()) == [
        "qr-first", "qr-second", "qr-third", "qr-fourth",
    ]
    assert detector.crop_calls


def test_complete_multi_decode_needs_no_extra_crops(monkeypatch):
    detector = _Detector(["a", "b", "c", "d"])
    monkeypatch.setattr(optical_reader._qr_detectors, "detector", detector, raising=False)
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(error=RuntimeError))
    assert optical_reader.decode_hdmi_qr(_Frame()) == ["a", "b", "c", "d"]
    assert detector.crop_calls == []




def test_duplicate_multi_results_do_not_hide_fourth_qr(monkeypatch):
    detector = _Detector(["qr-first", "qr-first", "qr-second", "qr-third"])
    monkeypatch.setattr(optical_reader._qr_detectors, "detector", detector, raising=False)
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(error=RuntimeError))

    assert optical_reader.decode_hdmi_qr(_Frame()) == [
        "qr-first", "qr-second", "qr-third", "qr-fourth",
    ]
    assert detector.crop_calls  # Duplicate cannot suppress missing-cell recovery.
