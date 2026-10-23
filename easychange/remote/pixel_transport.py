"""ECP1 visual transport for existing EC2/EC2H packets over HDMI.

This is a *display codec*, not a command channel. It never sends bytes through
Bluetooth, USB, sockets, or keyboard reports. Every recovered payload must
still pass the existing EC2 identity, CRC and result verification stages.
"""
from __future__ import annotations

import binascii
import struct

SIDE = 56
LARGE_SIDE = 72
DATA_START = 3
DATA_SIDE = SIDE - 2 * DATA_START
MAGIC = b"ECP1"
HEADER_BYTES = 10
MAX_PAYLOAD = (DATA_SIDE * DATA_SIDE) // 8 - HEADER_BYTES
LARGE_MAX_PAYLOAD = ((LARGE_SIDE - 2 * DATA_START) ** 2) // 8 - HEADER_BYTES


def encode_pixel_bitmap(text: str, *, side: int = SIDE) -> bytes:
    """Return 56x56 or 72x72 grayscale pixels without changing ECP1 wire bytes."""
    if type(side) is not int or side not in (SIDE, LARGE_SIDE):
        raise ValueError("PIXEL_SIDE_INVALID")
    if not isinstance(text, str):
        raise TypeError("PIXEL_PACKET_MUST_BE_TEXT")
    data_side = side - 2 * DATA_START
    capacity = data_side * data_side // 8 - HEADER_BYTES
    payload = text.encode("utf-8")
    if not payload or len(payload) > capacity:
        raise ValueError("PIXEL_PACKET_CAPACITY_EXCEEDED")
    body = MAGIC + struct.pack(">HI", len(payload), binascii.crc32(payload) & 0xffffffff) + payload
    bitmap = bytearray([255]) * (side * side)
    # White outer quiet zone, solid black registration ring, white separator.
    for y in range(1, side - 1):
        for x in range(1, side - 1):
            if x == 1 or x == side - 2 or y == 1 or y == side - 2:
                bitmap[y * side + x] = 0
    # The background is already white: only transmitted bits can be black.
    # Avoid scanning the unused capacity on small EC2/EC2H pages.
    for index in range(len(body) * 8):
        if body[index // 8] & (0x80 >> (index % 8)):
            x = DATA_START + index % data_side
            y = DATA_START + index // data_side
            bitmap[y * side + x] = 0
    return bytes(bitmap)


def decode_pixel_bitmap(bitmap, *, side: int = SIDE) -> str | None:
    """Validate registration, length and CRC on either bounded ECP1 cell."""
    if type(side) is not int or side not in (SIDE, LARGE_SIDE):
        return None
    data_side = side - 2 * DATA_START
    capacity = data_side * data_side // 8 - HEADER_BYTES
    try:
        if len(bitmap) != side * side:
            return None
        dark = lambda x, y: int(bitmap[y * side + x]) < 128
        for n in (3, 8, 20, 35, side - 4):
            if not (dark(n, 1) and dark(n, side - 2)
                    and dark(1, n) and dark(side - 2, n)):
                return None
            if dark(n, 0) or dark(n, side - 1) or dark(0, n) or dark(side - 1, n):
                return None
            if dark(n, 2) or dark(n, side - 3) or dark(2, n) or dark(side - 3, n):
                return None
        # Reject incorrect magic/length after the 10-byte header rather
        # than unpacking every unused module from every candidate contour.
        header = bytearray(HEADER_BYTES)
        for index in range(HEADER_BYTES * 8):
            x = DATA_START + index % data_side
            y = DATA_START + index // data_side
            if dark(x, y):
                header[index // 8] |= 0x80 >> (index % 8)
        if header[:4] != MAGIC:
            return None
        length, crc = struct.unpack(">HI", header[4:HEADER_BYTES])
        if not 0 < length <= capacity:
            return None
        payload = bytearray(length)
        for bit in range(length * 8):
            index = HEADER_BYTES * 8 + bit
            x = DATA_START + index % data_side
            y = DATA_START + index // data_side
            if dark(x, y):
                payload[bit // 8] |= 0x80 >> (bit % 8)
        raw = bytes(payload)
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
            # A 56x56 ECP1 symbol is still decodable around 105px across.
            # The previous 120px floor rejected legitimate 110px cells.
            if 100 <= w <= 370 and 100 <= h <= 370 and abs(w - h) <= max(7, w // 12):
                candidates.append((x, y, w, h))
        # Descending size makes real full-size tiles outrank small nested blobs.
        candidates.sort(key=lambda item: item[2] * item[3], reverse=True)
        found = []
        seen = set()
        for x, y, w, h in candidates[:120]:
            cropped = gray[y:y+h, x:x+w]
            text = None
            # On a white UI, the external quiet zone merges with the page
            # background. A contour may begin at the black registration ring
            # and exclude 1px of the encoded symbol. Preserve the original
            # zero-padding path for existing dark-background screenshots;
            # retry with exactly 1px border only if all CRCs failed.
            candidates_to_decode = [cropped]
            if x > 0 and y > 0 and x+w < width and y+h < height:
                candidates_to_decode.append(gray[y-1:y+h+1, x-1:x+w+1])
            for image in candidates_to_decode:
                # Old 56px ECP1 first; 72px compact is a CRC-checked fallback.
                for side in (SIDE, LARGE_SIDE):
                    exact = getattr(cv2, "INTER_NEAREST_EXACT", cv2.INTER_NEAREST)
                    for interpolation in (exact, cv2.INTER_AREA, cv2.INTER_LINEAR):
                        small = cv2.resize(image, (side, side), interpolation=interpolation)
                        text = decode_pixel_bitmap(small.ravel(), side=side)
                        if text is not None:
                            break
                    if text is not None:
                        break
                if text is not None:
                    break
            if text is not None and text not in seen:
                found.append(text)
                seen.add(text)
                if len(found) >= 32:
                    break
        return found
    except (cv2.error, ValueError, TypeError, MemoryError):
        return []
