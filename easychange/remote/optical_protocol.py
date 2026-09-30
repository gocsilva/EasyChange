from __future__ import annotations

import base64
import binascii
import hashlib
import json
import zlib
from typing import Any

PROTOCOL = "EC2"
DEFAULT_CHUNK_CHARS = 260


def encode_result_chunks(result: Any, *, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """Encode a complete command result into bounded QR-friendly EC2 packets."""
    if chunk_chars < 120:
        raise ValueError("chunk_chars must be >= 120")
    value = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(raw, level=6)
    token = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    pieces = [token[i:i + chunk_chars] for i in range(0, len(token), chunk_chars)] or [""]
    digest = hashlib.sha256(raw).hexdigest()[:16]
    crc = f"{binascii.crc32(raw) & 0xffffffff:08x}"
    sequence = str(value.get("sequence") or "-")
    status = "OK" if value.get("ok") else "ERR"
    total = len(pieces)
    return [
        json.dumps({
            "p": PROTOCOL, "v": 2, "rt": "result",
            "s": sequence, "st": status,
            "i": index, "n": total, "h": digest, "c": crc,
            "e": "zlib+b64u", "d": piece,
        }, ensure_ascii=False, separators=(",", ":"))
        for index, piece in enumerate(pieces)
    ]


def decode_result_chunks(payloads: list[str]) -> dict[str, Any]:
    """Reassemble and verify EC2 packets; identical duplicate chunks are idempotent."""
    if not payloads:
        raise ValueError("no optical chunks")
    chunks = [json.loads(item) for item in payloads]
    first = chunks[0]
    if first.get("p") != PROTOCOL:
        raise ValueError("unsupported optical protocol")
    identity = (first.get("s"), first.get("n"), first.get("h"), first.get("c"), first.get("e"))
    by_index: dict[int, str] = {}
    for chunk in chunks:
        if (chunk.get("s"), chunk.get("n"), chunk.get("h"), chunk.get("c"), chunk.get("e")) != identity:
            raise ValueError("mixed optical packet set")
        index = int(chunk["i"])
        data = str(chunk.get("d") or "")
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
