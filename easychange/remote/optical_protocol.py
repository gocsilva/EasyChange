from __future__ import annotations

import base64
import binascii
import hashlib
import json
import zlib
from typing import Any

PROTOCOL = "EC2"
HEADER_PROTOCOL = "EC2H"
DEFAULT_CHUNK_CHARS = 180


def _result_value(result: Any) -> dict[str, Any]:
    value = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    if not isinstance(value, dict):
        raise ValueError("optical result object required")
    return value


def _encode_raw(value: dict[str, Any]) -> tuple[bytes, bytes, str]:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(raw, level=6)
    token = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    return raw, compressed, token


def adaptive_chunk_chars(token_chars: int) -> int:
    """Conservative QR chunk sizing: denser only for small/medium payloads."""
    size = max(0, int(token_chars))
    if size <= 1200:
        return 260
    if size <= 4000:
        return 220
    return DEFAULT_CHUNK_CHARS


def encode_result_chunks(result: Any, *, chunk_chars: int | None = None) -> list[str]:
    """Encode a complete command result into bounded QR-friendly EC2 packets."""
    value = _result_value(result)
    raw, _compressed, token = _encode_raw(value)
    size = adaptive_chunk_chars(len(token)) if chunk_chars is None else int(chunk_chars)
    if size < 120:
        raise ValueError("chunk_chars must be >= 120")
    pieces = [token[i:i + size] for i in range(0, len(token), size)] or [""]
    digest = hashlib.sha256(raw).hexdigest()[:16]
    crc = f"{binascii.crc32(raw) & 0xffffffff:08x}"
    sequence = str(value.get("sequence") or "-")
    total = len(pieces)
    return [
        json.dumps({
            "p": PROTOCOL,
            "v": 2,
            "s": sequence,
            "i": index,
            "n": total,
            "h": digest,
            "c": crc,
            "x": f"{binascii.crc32(piece.encode('ascii')) & 0xffffffff:08x}",
            "d": piece,
        }, ensure_ascii=False, separators=(",", ":"))
        for index, piece in enumerate(pieces)
    ]


def encode_result_header(result: Any, *, chunk_count: int | None = None) -> str:
    """Small status-first QR that can be read before the full EC2 payload."""
    value = _result_value(result)
    raw, _compressed, token = _encode_raw(value)
    data = value.get("data") if isinstance(value.get("data"), dict) else {}
    receipt = data.get("mutation_receipt") if isinstance(data.get("mutation_receipt"), dict) else {}
    if chunk_count is None:
        chunk_count = max(1, (len(token) + adaptive_chunk_chars(len(token)) - 1) // adaptive_chunk_chars(len(token)))
    header = {
        "p": HEADER_PROTOCOL,
        "v": 1,
        "s": str(value.get("sequence") or "-"),
        "f": True,
        "ok": bool(value.get("ok")),
        "rc": value.get("code"),
        "l": len(raw),
        "n": int(chunk_count),
        "h": hashlib.sha256(raw).hexdigest(),
        "m": receipt.get("lifecycle_state") or receipt.get("state"),
        "cid": value.get("command_id"),
    }
    return json.dumps(header, ensure_ascii=False, separators=(",", ":"))


def decode_result_chunks(payloads: list[str]) -> dict[str, Any]:
    """Reassemble and verify EC2 packets; identical duplicate chunks are idempotent."""
    if not payloads:
        raise ValueError("no optical chunks")
    chunks = [json.loads(item) for item in payloads]
    chunks = [chunk for chunk in chunks if isinstance(chunk, dict) and chunk.get("p") == PROTOCOL]
    if not chunks:
        raise ValueError("no EC2 optical chunks")
    first = chunks[0]
    identity = (first.get("s"), first.get("n"), first.get("h"), first.get("c"), first.get("e"))
    by_index: dict[int, str] = {}
    for chunk in chunks:
        if (chunk.get("s"), chunk.get("n"), chunk.get("h"), chunk.get("c"), chunk.get("e")) != identity:
            raise ValueError("mixed optical packet set")
        index = int(chunk["i"])
        data = str(chunk.get("d") or "")
        fragment_crc = chunk.get("x")
        if fragment_crc and f"{binascii.crc32(data.encode('ascii')) & 0xffffffff:08x}" != str(fragment_crc):
            raise ValueError(f"optical chunk crc32 mismatch: {index}")
        previous = by_index.get(index)
        if previous is not None and previous != data:
            raise ValueError("conflicting duplicate optical chunk")
        by_index[index] = data
    total = int(first["n"])
    if sorted(by_index) != list(range(total)):
        raise ValueError(f"incomplete optical packet set: {len(by_index)}/{total}")
    token = "".join(by_index[index] for index in range(total))
    compressed = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    raw = zlib.decompress(compressed)
    if hashlib.sha256(raw).hexdigest()[:16] != first["h"]:
        raise ValueError("optical sha256 mismatch")
    if f"{binascii.crc32(raw) & 0xffffffff:08x}" != first["c"]:
        raise ValueError("optical crc32 mismatch")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("optical result object required")
    return value
