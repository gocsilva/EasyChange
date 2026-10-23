"""Compact, integrity-checked EC1 payload wrapper for single-line HID entry."""
from __future__ import annotations

import base64
import binascii
import zlib


def encode_command(command: str, threshold_bytes: int = 256) -> tuple[str, dict]:
    if type(command) is not str:
        raise ValueError("INVALID_EC1_COMMAND")
    try:
        raw = command.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("EC1_COMMAND_INVALID_UTF8") from exc
    # Keep the sender's limit aligned with decode_command's default budget.
    # Otherwise compressible oversized commands produce undecodable :z1 frames.
    if len(raw) > 262144:
        raise ValueError("EC1_COMMAND_SIZE_LIMIT")
    metadata = {"encoding": "raw", "raw_bytes": len(raw), "wire_bytes": len(raw), "crc32": f"{binascii.crc32(raw) & 0xffffffff:08x}"}
    if len(raw) < max(0, int(threshold_bytes)):
        return command, metadata
    compressed = zlib.compress(raw, level=6)
    token = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    packet = f":z1 {len(raw)} {binascii.crc32(raw) & 0xffffffff:08x} {token}"
    wire = len(packet.encode("ascii"))
    if wire >= len(raw):
        return command, metadata
    return packet, {"encoding": "zlib+base64url", "raw_bytes": len(raw), "wire_bytes": wire,
                    "compressed_bytes": len(compressed), "crc32": f"{binascii.crc32(raw) & 0xffffffff:08x}"}


def decode_command(packet: str, max_raw_bytes: int = 262144) -> tuple[str, dict]:
    if type(packet) is not str:
        raise ValueError("INVALID_EC1_Z1_PACKET")
    parts = packet.split(" ", 3)
    if len(parts) != 4 or parts[0].casefold() != ":z1":
        raise ValueError("INVALID_EC1_Z1_PACKET")
    # Header values are untrusted HID text. Limit decimal and CRC fields
    # before int() parses them: enormous numeric strings can consume CPU
    # even when the decoded command budget is only a few hundred KiB.
    if (not 1 <= len(parts[1]) <= 12 or len(parts[2]) != 8
            or any(ch not in "0123456789abcdefABCDEF" for ch in parts[2])):
        raise ValueError("INVALID_EC1_Z1_HEADER")
    try:
        expected_size = int(parts[1])
        expected_crc = int(parts[2], 16)
    except (TypeError, ValueError) as exc:
        raise ValueError("INVALID_EC1_Z1_HEADER") from exc
    if expected_size < 0 or expected_size > int(max_raw_bytes):
        raise ValueError("EC1_Z1_SIZE_LIMIT")
    token = parts[3]
    # A zlib stream of at most max_raw_bytes cannot legitimately exceed this
    # generous compressed framing budget. Reject oversize input before
    # allocating a decoded base64 buffer.
    max_token_chars = 4 * ((int(max_raw_bytes) + 1024 + 2) // 3) + 8
    if len(token) > max_token_chars:
        raise ValueError("EC1_Z1_COMPRESSED_SIZE_LIMIT")
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
