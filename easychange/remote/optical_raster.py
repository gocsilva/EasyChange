"""Worker-safe EC2 QR/ECP1 rasterization.

Pure pixel/QR generation: never touches QWidget, QPixmap, capture, HID or
network. A GUI thread alone converts these raster bytes into QPixmaps.
"""
from __future__ import annotations
import json

from easychange.remote.pixel_transport import (
    SIDE, LARGE_SIDE, encode_pixel_bitmap,
)


def rasterize_optical_payload(
    payload: str, requested: str, host_preference: str,
    *, burst: bool, label_side: int,
) -> tuple[bytes, int, str, str]:
    """Return (8-bit pixels, side, codec, actual transmitted EC2 wire).

    Do not rescale/crop data, alter EC2/CRC fields or lower bitmap resolution.
    The caller scales the final preview/presentation at its actual Qt size.
    """
    optical_mode = host_preference if requested == "AUTO" else requested
    wire = payload
    try:
        packet = json.loads(wire)
        if isinstance(packet, dict) and packet.get("p") == "EC2H":
            packet["tm"] = optical_mode
            packet["rm"] = requested
            packet["vh"] = 2
            wire = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError):
        pass

    as_pixel = optical_mode in {"PIXEL", "PIXEL+QR", "HYBRID"}
    if as_pixel and optical_mode in {"PIXEL+QR", "HYBRID"}:
        try:
            packet = json.loads(wire)
            as_pixel = (
                packet.get("p") == "EC2H"
                or (
                    packet.get("p") == "EC2"
                    and int(packet.get("i", 0)) % 4 != 0
                )
            )
        except (ValueError, TypeError, OverflowError, AttributeError):
            as_pixel = False

    if as_pixel:
        try:
            return encode_pixel_bitmap(wire), SIDE, "PIXEL", wire
        except ValueError:
            if not burst and label_side >= 170:
                try:
                    return encode_pixel_bitmap(wire, side=LARGE_SIDE), LARGE_SIDE, "PIXEL", wire
                except ValueError:
                    pass
    # Exact legacy QR algorithm and mask: preserving module geometry,
    # contrast, border and order is more important than a lossy speed hack.
    import qrcode
    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1, border=4, mask_pattern=3,
    )
    code.add_data(wire)
    code.make(fit=True)
    matrix = code.get_matrix()
    side = len(matrix)
    if side == 0 or any(len(row) != side for row in matrix):
        raise ValueError("INVALID_OPTICAL_QR_MATRIX")
    pixels = bytes(0 if dark else 255 for row in matrix for dark in row)
    return pixels, side, "QR", wire
