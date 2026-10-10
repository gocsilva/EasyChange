from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import zlib
from typing import Any

PROTOCOL = "EC2"
HEADER_PROTOCOL = "EC2H"
DEFAULT_CHUNK_CHARS = 180
OPTICAL_COMPRESSION_LEVEL = 3
# Decoder limits apply to untrusted QR strings, not to durable on-disk results.
MAX_EC2_PACKETS = 200_000
MAX_EC2_PACKET_TEXT_CHARS = 65_536
# Includes envelope overhead and duplicates: cap the complete untrusted QR set.
MAX_EC2_PACKET_SET_CHARS = 64 * 1024 * 1024
MAX_EC2_TOKEN_CHARS = 16 * 1024 * 1024
MAX_EC2_COMPRESSED_BYTES = 12 * 1024 * 1024
MAX_EC2_RESULT_BYTES = 16 * 1024 * 1024


def _result_value(result: Any) -> dict[str, Any]:
    # Optical transport never mutates Result.data. Prefer the shallow wire
    # projection and avoid recursively duplicating multi-megabyte payloads.
    if hasattr(result, "to_wire_dict"):
        value = result.to_wire_dict()
    else:
        fields = getattr(result, "__dataclass_fields__", None)
        if isinstance(fields, dict):
            value = {name: getattr(result, name) for name in fields}
        elif hasattr(result, "to_dict"):
            value = result.to_dict()
        else:
            value = dict(result)
    if not isinstance(value, dict):
        raise ValueError("optical result object required")
    return value


def _compact_transport_value(value: dict[str, Any]) -> dict[str, Any]:
    """Remove reversible structured-response duplication without copying payloads."""
    data = value.get("data")
    if not isinstance(data, dict):
        return value
    results = data.get("results")
    primary = data.get("primary")
    if not (
        isinstance(results, list)
        and len(results) == 1
        and isinstance(results[0], dict)
        and isinstance(primary, dict)
        and primary == results[0]
    ):
        return value
    compact = dict(value)
    compact_data = dict(data)
    compact_data["primary"] = {"$ec_ref": "results[0]"}
    compact_data["transport_compacted"] = True
    compact["data"] = compact_data
    return compact


def _expand_transport_value(value: dict[str, Any]) -> dict[str, Any]:
    data = value.get("data")
    if not isinstance(data, dict):
        return value
    primary = data.get("primary")
    results = data.get("results")
    if (
        isinstance(primary, dict)
        and primary.get("$ec_ref") == "results[0]"
        and isinstance(results, list)
        and len(results) >= 1
        and isinstance(results[0], dict)
    ):
        data["primary"] = copy.deepcopy(results[0])
        data.pop("transport_compacted", None)
    return value


def _encode_raw(value: dict[str, Any]) -> tuple[bytes, bytes, str]:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # Only the first optical page is broadcast for large results; the rest
    # is recovered from the durable ResultStore. Level 3 cuts bundle preparation
    # CPU substantially versus level 6 while keeping QR density nearly identical.
    compressed = zlib.compress(raw, level=OPTICAL_COMPRESSION_LEVEL)
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


def _transport_components(
    result: Any,
    *,
    chunk_chars: int | None = None,
) -> tuple[dict[str, Any], bytes, str, int, int, str, str, str]:
    value = _result_value(result)
    wire_value = _compact_transport_value(value)
    raw, _compressed, token = _encode_raw(wire_value)
    size = adaptive_chunk_chars(len(token)) if chunk_chars is None else int(chunk_chars)
    if size < 120:
        raise ValueError("chunk_chars must be >= 120")
    total = max(1, (len(token) + size - 1) // size)
    digest = hashlib.sha256(raw).hexdigest()
    crc = f"{binascii.crc32(raw) & 0xffffffff:08x}"
    sequence = str(value.get("sequence") or "-")
    return value, raw, token, size, total, digest, crc, sequence


def _packet_json(
    *, sequence: str, index: int, total: int, digest: str, crc: str, piece: str,
) -> str:
    return json.dumps({
        "p": PROTOCOL, "v": 2, "s": sequence, "i": index, "n": total,
        "h": digest[:16], "c": crc,
        "x": f"{binascii.crc32(piece.encode('ascii')) & 0xffffffff:08x}",
        "d": piece,
    }, ensure_ascii=False, separators=(",", ":"))


def _header_json(
    value: dict[str, Any], raw: bytes, *, total: int, digest: str,
    broadcast_count: int | None = None,
) -> str:
    data = value.get("data") if isinstance(value.get("data"), dict) else {}
    receipt = data.get("mutation_receipt") if isinstance(data.get("mutation_receipt"), dict) else {}
    header = {
        "p": HEADER_PROTOCOL, "v": 1,
        "s": str(value.get("sequence") or "-"), "f": True,
        "ok": bool(value.get("ok")), "rc": value.get("code"),
        "l": len(raw), "n": int(total), "h": digest,
        "m": receipt.get("lifecycle_state") or receipt.get("state"),
        "cid": value.get("command_id"),
    }
    if broadcast_count is not None:
        header["w"] = max(0, min(int(total), int(broadcast_count)))
    return json.dumps(header, ensure_ascii=False, separators=(",", ":"))



def encode_durable_result_header(header: dict[str, Any]) -> str:
    """Encode a terminal EC2H receipt without serializing/compressing the payload.

    This is intentionally durable-only: n/w stay zero until the background
    optical bundle knows the real compressed EC2 packet count.
    """
    if not isinstance(header, dict):
        raise ValueError("durable result header object required")
    sequence = str(header.get("sequence") or "")
    if not sequence:
        raise ValueError("durable result sequence required")
    if header.get("finished") is not True:
        raise ValueError("durable result is not terminal")
    try:
        payload_length = max(0, int(header.get("payload_length") or 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid durable result payload length") from exc
    mutation_state = (
        header.get("mutation_state") if bool(header.get("mutation")) else None
    )
    return json.dumps({
        "p": HEADER_PROTOCOL, "v": 1, "s": sequence, "f": True,
        "ok": bool(header.get("success")), "rc": header.get("return_code"),
        "l": payload_length, "n": 0, "h": str(header.get("sha256") or ""),
        "m": mutation_state, "cid": header.get("command_id"),
        "w": 0, "d": 1,
    }, ensure_ascii=False, separators=(",", ":"))
def encode_result_chunks(
    result: Any,
    *,
    chunk_chars: int | None = None,
    max_packets: int | None = None,
    indexes: list[int] | tuple[int, ...] | None = None,
) -> list[str]:
    """Encode only the EC2 packet window/indexes that are actually needed."""
    _value, _raw, token, size, total, digest, crc, sequence = _transport_components(
        result, chunk_chars=chunk_chars
    )
    if indexes is None:
        materialized = total if max_packets is None else max(1, min(total, int(max_packets)))
        selected = list(range(materialized))
    else:
        # Index requests may originate from remote optical retransmissions.
        # Bound the raw input and use O(1) deduplication instead of a growing
        # list membership scan (O(n^2) on long sparse windows).
        if not isinstance(indexes, (list, tuple)) or len(indexes) > MAX_EC2_PACKETS:
            raise ValueError("OPTICAL_CHUNK_INDEX_REQUEST_TOO_LARGE")
        selected: list[int] = []
        seen: set[int] = set()
        for raw_index in indexes:
            if (type(raw_index) is not int and
                    not (isinstance(raw_index, str) and len(raw_index) <= 32
                         and raw_index.strip().isdecimal())):
                raise ValueError("invalid optical chunk index")
            index = int(raw_index)
            if index < 0 or index >= total:
                raise IndexError("OPTICAL_CHUNK_OUT_OF_RANGE")
            if index not in seen:
                seen.add(index)
                selected.append(index)
        if not selected:
            raise ValueError("at least one optical chunk index is required")
        if max_packets is not None:
            selected = selected[:max(1, int(max_packets))]
    packets: list[str] = []
    for index in selected:
        piece = token[index * size:(index + 1) * size]
        packets.append(_packet_json(
            sequence=sequence, index=index, total=total,
            digest=digest, crc=crc, piece=piece,
        ))
    return packets


def encode_result_header(result: Any, *, chunk_count: int | None = None) -> str:
    value, raw, _token, _size, total, digest, _crc, _sequence = _transport_components(result)
    return _header_json(
        value, raw,
        total=total if chunk_count is None else int(chunk_count),
        digest=digest,
    )


def encode_result_bundle(
    result: Any,
    *,
    chunk_chars: int | None = None,
    max_packets: int | None = None,
    overflow_packets: int | None = None,
) -> tuple[list[str], str]:
    """Encode broadcast packets and EC2H in one serialization/compression pass."""
    value, raw, token, size, total, digest, crc, sequence = _transport_components(
        result, chunk_chars=chunk_chars
    )
    if max_packets is None:
        materialized = total
    else:
        cap = max(1, min(total, int(max_packets)))
        if overflow_packets is not None and total > int(max_packets):
            materialized = max(1, min(cap, int(overflow_packets)))
        else:
            materialized = cap
    packets: list[str] = []
    for index in range(materialized):
        piece = token[index * size:(index + 1) * size]
        packets.append(_packet_json(
            sequence=sequence, index=index, total=total,
            digest=digest, crc=crc, piece=piece,
        ))
    return packets, _header_json(
        value, raw, total=total, digest=digest, broadcast_count=materialized
    )


def decode_result_chunks(payloads: list[str]) -> dict[str, Any]:
    """Reassemble bounded EC2 packets and reject malformed or oversized QR data."""
    if not payloads:
        raise ValueError("no optical chunks")
    if len(payloads) > MAX_EC2_PACKETS * 2:
        raise ValueError("too many optical packets")
    first: dict[str, Any] | None = None
    identity: tuple[Any, ...] | None = None
    total = 0
    by_index: dict[int, str] = {}
    token_chars = 0
    packet_text_chars = 0
    for raw_packet in payloads:
        if not isinstance(raw_packet, str) or len(raw_packet) > MAX_EC2_PACKET_TEXT_CHARS:
            raise ValueError("optical packet too large")
        # Check *before* json.loads() and include ignored or repeated frames.
        # Otherwise many individually small QR packets can exhaust memory/CPU.
        packet_text_chars += len(raw_packet)
        if packet_text_chars > MAX_EC2_PACKET_SET_CHARS:
            raise ValueError("optical packet set too large")
        try:
            chunk = json.loads(raw_packet)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("invalid optical packet JSON") from exc
        if not isinstance(chunk, dict) or chunk.get("p") != PROTOCOL:
            continue
        if first is None:
            first = chunk
            total = chunk.get("n")
            if type(total) is not int or total < 1 or total > MAX_EC2_PACKETS:
                raise ValueError("invalid optical packet count")
            identity = (chunk.get("s"), total, chunk.get("h"),
                        chunk.get("c"), chunk.get("e"))
        elif (chunk.get("s"), chunk.get("n"), chunk.get("h"),
              chunk.get("c"), chunk.get("e")) != identity:
            raise ValueError("mixed optical packet set")
        index = chunk.get("i")
        if type(index) is not int or not 0 <= index < total:
            raise ValueError("invalid optical chunk index")
        data = chunk.get("d")
        if not isinstance(data, str):
            raise ValueError("invalid optical chunk data")
        try:
            data_bytes = data.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("invalid optical chunk encoding") from exc
        if any(char not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in data_bytes):
            raise ValueError("invalid optical base64url fragment")
        fragment_crc = chunk.get("x")
        if fragment_crc and f"{binascii.crc32(data_bytes) & 0xffffffff:08x}" != str(fragment_crc):
            raise ValueError(f"optical chunk crc32 mismatch: {index}")
        previous = by_index.get(index)
        if previous is not None:
            if previous != data:
                raise ValueError("conflicting duplicate optical chunk")
            continue
        token_chars += len(data)
        if token_chars > MAX_EC2_TOKEN_CHARS:
            raise ValueError("optical token too large")
        by_index[index] = data
    if first is None:
        raise ValueError("no EC2 optical chunks")
    # Every unique index was already range checked. A full set needs only
    # a cardinality check; never allocate list(range(untrusted_total)).
    if len(by_index) != total:
        raise ValueError(f"incomplete optical packet set: {len(by_index)}/{total}")
    token = "".join(by_index[index] for index in range(total))
    try:
        compressed = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid optical base64 token") from exc
    if len(compressed) > MAX_EC2_COMPRESSED_BYTES:
        raise ValueError("optical compressed result too large")
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_EC2_RESULT_BYTES + 1)
        if len(raw) > MAX_EC2_RESULT_BYTES or decoder.unconsumed_tail:
            raise ValueError("optical decompressed result too large")
        raw += decoder.flush(MAX_EC2_RESULT_BYTES + 1 - len(raw))
        if len(raw) > MAX_EC2_RESULT_BYTES:
            raise ValueError("optical decompressed result too large")
        if not decoder.eof or decoder.unused_data:
            raise ValueError("invalid optical compressed stream")
    except zlib.error as exc:
        raise ValueError("invalid optical compressed stream") from exc
    if hashlib.sha256(raw).hexdigest()[:16] != first["h"]:
        raise ValueError("optical sha256 mismatch")
    if f"{binascii.crc32(raw) & 0xffffffff:08x}" != first["c"]:
        raise ValueError("optical crc32 mismatch")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("optical result object required")
    return _expand_transport_value(value)
