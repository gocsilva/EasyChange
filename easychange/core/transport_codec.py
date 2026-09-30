"""Compact, integrity-checked EC1 payload wrapper for single-line HID entry."""
from __future__ import annotations

import base64
import binascii
import zlib


def encode_command(command: str, threshold_bytes: int = 256) -> tuple[str, dict]:
    raw = str(command).encode("utf-8")
    metadata = {"encoding": "raw", "raw_bytes": len(raw), "wire_bytes": len(raw), "crc32": f"{binascii.crc32(raw) & 0xffffffff:08x}"}
    if len(raw) < max(0, int(threshold_bytes)):
        return str(command), metadata
    compressed = zlib.compress(raw, level=6)
    token = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    packet = f":z1 {len(raw)} {binascii.crc32(raw) & 0xffffffff:08x} {token}"
    wire = len(packet.encode("ascii"))
    if wire >= len(raw):
        return str(command), metadata
    return packet, {"encoding": "zlib+base64url", "raw_bytes": len(raw), "wire_bytes": wire,
                    "compressed_bytes": len(compressed), "crc32": f"{binascii.crc32(raw) & 0xffffffff:08x}"}


def decode_command(packet: str, max_raw_bytes: int = 262144) -> tuple[str, dict]:
    parts = str(packet).split(" ", 3)
    if len(parts) != 4 or parts[0].casefold() != ":z1":
        raise ValueError("INVALID_EC1_Z1_PACKET")
    try:
        expected_size = int(parts[1])
        expected_crc = int(parts[2], 16)
    except (TypeError, ValueError) as exc:
        raise ValueError("INVALID_EC1_Z1_HEADER") from exc
    if expected_size < 0 or expected_size > int(max_raw_bytes):
        raise ValueError("EC1_Z1_SIZE_LIMIT")
    token = parts[3]
    if not token or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in token):
        raise ValueError("INVALID_EC1_Z1_BASE64URL")
    try:
        compressed = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, int(max_raw_bytes) + 1)
        if len(raw) > int(max_raw_bytes) or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
            raise ValueError("EC1_Z1_DECOMPRESSION_LIMIT_OR_TRAILING_DATA")
    except (ValueError, zlib.error, binascii.Error) as exc:
        raise ValueError("EC1_Z1_DECODE_FAILED") from exc
    if len(raw) != expected_size:
        raise ValueError("EC1_Z1_LENGTH_MISMATCH")
    if (binascii.crc32(raw) & 0xffffffff) != expected_crc:
        raise ValueError("EC1_Z1_CRC_MISMATCH")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("EC1_Z1_INVALID_UTF8") from exc
    return text, {"encoding": "zlib+base64url", "raw_bytes": len(raw), "wire_bytes": len(packet.encode("ascii")),
                  "crc32": f"{expected_crc:08x}"}
