"""ECP1 visual transport for existing EC2/EC2H packets over HDMI.

This is a *display codec*, not a command channel. It never sends bytes through
Bluetooth, USB, sockets, or keyboard reports. Every recovered payload must
still pass the existing EC2 identity, CRC and result verification stages.
"""
from __future__ import annotations

import binascii
import struct

SIDE = 56
DATA_START = 3
DATA_SIDE = SIDE - 2 * DATA_START
MAGIC = b"ECP1"
HEADER_BYTES = 10
MAX_PAYLOAD = (DATA_SIDE * DATA_SIDE) // 8 - HEADER_BYTES


def encode_pixel_bitmap(text: str) -> bytes:
    """Return 56x56 grayscale image pixels, black=0 and white=255."""
    if not isinstance(text, str):
        raise TypeError("PIXEL_PACKET_MUST_BE_TEXT")
    payload = text.encode("utf-8")
    if not payload or len(payload) > MAX_PAYLOAD:
        raise ValueError("PIXEL_PACKET_CAPACITY_EXCEEDED")
    body = MAGIC + struct.pack(">HI", len(payload), binascii.crc32(payload) & 0xffffffff) + payload
    bitmap = bytearray([255]) * (SIDE * SIDE)
    # White outer quiet zone, solid black registration ring, white separator.
    for y in range(1, SIDE - 1):
        for x in range(1, SIDE - 1):
            if x == 1 or x == SIDE - 2 or y == 1 or y == SIDE - 2:
                bitmap[y * SIDE + x] = 0
    for index in range(DATA_SIDE * DATA_SIDE):
        byte_index = index // 8
        black = bool(byte_index < len(body) and body[byte_index] & (0x80 >> (index % 8)))
        if black:
            x = DATA_START + index % DATA_SIDE
            y = DATA_START + index // DATA_SIDE
            bitmap[y * SIDE + x] = 0
    return bytes(bitmap)


def decode_pixel_bitmap(bitmap) -> str | None:
    """Validate registration, length and CRC; accept no unauthenticated payload."""
    try:
        if len(bitmap) != SIDE * SIDE:
            return None
        dark = lambda x, y: int(bitmap[y * SIDE + x]) < 128
        for n in (3, 8, 20, 35, SIDE - 4):
            if not (dark(n, 1) and dark(n, SIDE - 2)
                    and dark(1, n) and dark(SIDE - 2, n)):
                return None
            if dark(n, 0) or dark(n, SIDE - 1) or dark(0, n) or dark(SIDE - 1, n):
                return None
            if dark(n, 2) or dark(n, SIDE - 3) or dark(2, n) or dark(SIDE - 3, n):
                return None
        data = bytearray((DATA_SIDE * DATA_SIDE + 7) // 8)
        for index in range(DATA_SIDE * DATA_SIDE):
            x = DATA_START + index % DATA_SIDE
            y = DATA_START + index // DATA_SIDE
            if dark(x, y):
                data[index // 8] |= 0x80 >> (index % 8)
        if data[:4] != MAGIC:
            return None
        length, crc = struct.unpack(">HI", data[4:HEADER_BYTES])
        if not 0 < length <= MAX_PAYLOAD:
            return None
        raw = bytes(data[HEADER_BYTES:HEADER_BYTES + length])
        if binascii.crc32(raw) & 0xffffffff != crc:
            return None
        return raw.decode("utf-8")
    except (IndexError, TypeError, ValueError, UnicodeDecodeError, struct.error):
        return None


def decode_pixel_frame(frame) -> list[str]:
    """Scan HDMI pixels for bounded ECP1 cells without assuming window position.

    Import OpenCV on demand (encoder does not depend on OpenCV). The external
    white registration ring is a contour; all data are decoded from observed
    pixels only. Never report a simulated or stale frame as a real result.
    """
    shape = getattr(frame, "shape", None)
    if not isinstance(shape, tuple) or len(shape) not in (2, 3):
        return []
    height, width = shape[:2]
    if not (64 <= height <= 8192 and 64 <= width <= 8192):
        return []
    import cv2
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(shape) == 3 else frame
        _, bright = cv2.threshold(gray, 190, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(bright, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if 120 <= w <= 370 and 120 <= h <= 370 and abs(w - h) <= max(7, w // 12):
                candidates.append((x, y, w, h))
        # Descending size makes real full-size tiles outrank small nested blobs.
        candidates.sort(key=lambda item: item[2] * item[3], reverse=True)
        found = []
        seen = set()
        for x, y, w, h in candidates[:120]:
            cropped = gray[y:y+h, x:x+w]
            small = cv2.resize(cropped, (SIDE, SIDE), interpolation=cv2.INTER_AREA)
            text = decode_pixel_bitmap(small.ravel())
            if text is not None and text not in seen:
                found.append(text)
                seen.add(text)
        return found[:32]
    except (cv2.error, ValueError, TypeError, MemoryError):
        return []
