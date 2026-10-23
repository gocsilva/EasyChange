from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import math
import zlib
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

PROTOCOL = "EC2"
HEADER_PROTOCOL = "EC2H"
DEFAULT_CHUNK_CHARS = 180
OPTICAL_COMPRESSION_LEVEL = 3
# Decoder limits apply to untrusted QR strings, not to durable on-disk results.
MAX_EC2_PACKETS = 200_000
MAX_EC2_PACKET_TEXT_CHARS = 65_536
# The desktop Qt renderer uses QR error correction M: Version 40 byte-mode
# capacity is 2331 bytes. Keep outgoing EC2 envelopes renderable as QR.
MAX_EC2_QR_RENDER_BYTES = 2331
# Includes envelope overhead and duplicates: cap the complete untrusted QR set.
MAX_EC2_PACKET_SET_CHARS = 64 * 1024 * 1024
MAX_EC2_TOKEN_CHARS = 16 * 1024 * 1024
MAX_EC2_COMPRESSED_BYTES = 12 * 1024 * 1024
MAX_EC2_RESULT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PreparedOpticalResult:
    """Immutable EC2 source reused while selectively recovering one result."""

    token: str
    chunk_chars: int
    total: int
    digest: str
    crc: str
    sequence: str
    literal_marker: bool

    @property
    def cache_bytes(self) -> int:
        return len(self.token) + 128


def prepare_optical_result(
    result: Any, *, chunk_chars: int | None = None
) -> PreparedOpticalResult:
    """Serialize/compress a result once for one or more selective page reads."""
    _value, _raw, token, size, total, digest, crc, sequence, literal_marker = (
        _transport_components(result, chunk_chars=chunk_chars)
    )
    return PreparedOpticalResult(
        token=token,
        chunk_chars=size,
        total=total,
        digest=digest,
        crc=crc,
        sequence=sequence,
        literal_marker=literal_marker,
    )


def valid_ec2_identity(packet: dict) -> bool:
    """The first QR cannot reserve an assembly using malformed hash metadata."""
    if not isinstance(packet, dict):
        return False
    seq, digest, crc = packet.get("s"), packet.get("h"), packet.get("c")
    alphabet = "0123456789abcdef"
    return (
        type(packet.get("v")) is int and packet["v"] == 2
        and isinstance(seq, str) and bool(seq) and len(seq) <= 256
        and not any(0xD800 <= ord(ch) <= 0xDFFF for ch in seq)
        and isinstance(digest, str) and len(digest) == 16
        and all(char in alphabet for char in digest)
        and isinstance(crc, str) and len(crc) == 8
        and all(char in alphabet for char in crc)
    )


def _unique_optical_json_fields(pairs):
    fields={}
    for key,value in pairs:
        if key in fields:
            raise ValueError("duplicate optical packet JSON field")
        fields[key]=value
    return fields


def _optical_float(value):
    number=float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite optical packet number")
    return number


def _optical_constant(_value):
    raise ValueError("nonfinite optical packet number")


def decode_ec2_packet_json(raw):
    """Parse untrusted QR envelopes without accepting ambiguous JSON fields."""
    try:
        return json.loads(
            raw,object_pairs_hook=_unique_optical_json_fields,
            parse_constant=_optical_constant,parse_float=_optical_float)
    except (ValueError,RecursionError,UnicodeError) as exc:
        raise ValueError("invalid optical packet JSON") from exc


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


def _json_equal_with_types(left: Any, right: Any) -> bool:
    """Compare JSON values without coercing True to 1 or copying wide arrays.

    Keep only a depth-first iterator stack and active ancestor pairs. A
    pending.extend(zip(...)) copies one pair for each element of a wide result
    before the optical byte-budget check, unnecessarily spiking peak memory.
    """
    def mapping_pairs(source: dict, destination: dict):
        # Bind the destination to this iterator, not the outer-loop "rhs"
        # variable (which changes as child scalars are compared).
        for key, value in source.items():
            yield value, destination[key]

    stack = [(iter(((left, right),)), None)]
    ancestors: set[tuple[int, int]] = set()
    while stack:
        iterator, owner = stack[-1]
        try:
            lhs, rhs = next(iterator)
        except StopIteration:
            stack.pop()
            if owner is not None:
                ancestors.remove(owner)
            continue
        if type(lhs) is not type(rhs):
            return False
        if isinstance(lhs, dict):
            # JSON stringifies non-string keys, potentially aliasing 1 and "1".
            if (any(type(key) is not str for key in lhs)
                    or lhs.keys() != rhs.keys()):
                return False
            pair = (id(lhs), id(rhs))
            if pair in ancestors:
                continue
            ancestors.add(pair)
            stack.append((mapping_pairs(lhs, rhs), pair))
        elif isinstance(lhs, list):
            if len(lhs) != len(rhs):
                return False
            pair = (id(lhs), id(rhs))
            if pair in ancestors:
                continue
            ancestors.add(pair)
            stack.append((zip(lhs, rhs), pair))
        elif lhs != rhs:
            return False
    return True


def _compact_transport_value(value: dict[str, Any]) -> dict[str, Any]:
    """Remove reversible structured-response duplication without copying payloads."""
    data = value.get("data")
    if not isinstance(data, dict):
        return value
    # A caller can legitimately own this field. Never overwrite its value
    # with a transport-only marker or drop it during expansion.
    if "transport_compacted" in data:
        return value
    results = data.get("results")
    primary = data.get("primary")
    if not (
        isinstance(results, list)
        and len(results) == 1
        and isinstance(results[0], dict)
        and isinstance(primary, dict)
        and _json_equal_with_types(primary, results[0])
    ):
        return value
    compact = dict(value)
    compact_data = dict(data)
    compact_data["primary"] = {"$ec_ref": "results[0]"}
    compact_data["transport_compacted"] = True
    compact["data"] = compact_data
    return compact


def _expand_transport_value(
    value: dict[str, Any], *, preserve_literal_marker: bool = False
) -> dict[str, Any]:
    data = value.get("data")
    if not isinstance(data, dict):
        return value
    primary = data.get("primary")
    results = data.get("results")
    if (
        not preserve_literal_marker
        and data.get("transport_compacted") is True
        and isinstance(primary, dict)
        and primary == {"$ec_ref":"results[0]"}
        and isinstance(results, list)
        and len(results) >= 1
        and isinstance(results[0], dict)
    ):
        data["primary"] = copy.deepcopy(results[0])
        data.pop("transport_compacted", None)
    return value


def _encode_raw(value: dict[str, Any]) -> tuple[bytes, bytes, str]:
    # Enforce the receiver's byte budget during JSON encoding. A full
    # json.dumps() copy of an oversized result would spike memory before the
    # compression/QR stages could reject the envelope.
    output=bytearray()
    encoder=json.JSONEncoder(ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"))
    for part in encoder.iterencode(value):
        # An encoder may emit one enormous JSON string token. Its character
        # count is a lower bound for UTF-8 bytes: reject before allocating
        # another unbounded full-token byte copy.
        remaining=MAX_EC2_RESULT_BYTES-len(output)
        if len(part)>remaining:
            raise ValueError("OPTICAL_RESULT_TOO_LARGE")
        for offset in range(0,len(part),8192):
            fragment=part[offset:offset+8192].encode("utf-8")
            if len(fragment)>MAX_EC2_RESULT_BYTES-len(output):
                raise ValueError("OPTICAL_RESULT_TOO_LARGE")
            output.extend(fragment)
    raw=bytes(output)
    # Only the first optical page is broadcast for large results; the rest
    # is recovered from the durable ResultStore. Level 3 cuts bundle preparation
    # CPU substantially versus level 6 while keeping QR density nearly identical.
    compressed=zlib.compress(raw,level=OPTICAL_COMPRESSION_LEVEL)
    if len(compressed)>MAX_EC2_COMPRESSED_BYTES:
        raise ValueError("OPTICAL_COMPRESSED_RESULT_TOO_LARGE")
    token=base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    if len(token)>MAX_EC2_TOKEN_CHARS:
        raise ValueError("OPTICAL_RESULT_TOKEN_TOO_LARGE")
    return raw,compressed,token


def adaptive_chunk_chars(token_chars: int) -> int:
    """Keep compact EC2 QR symbols legible without adding a fifth cell."""
    size = max(0, int(token_chars))
    # At most four packets fit the compact row. Smaller QR versions preserve
    # module edges better after Windows DPI and HDMI resampling; <=800 token
    # chars still fits in the existing compact row with 200-char fragments.
    if size <= 800:
        return 200
    if size <= 1200:
        return 260
    if size <= 4000:
        return 220
    return DEFAULT_CHUNK_CHARS


def _transport_components(
    result: Any,
    *,
    chunk_chars: int | None = None,
) -> tuple[dict[str, Any], bytes, str, int, int, str, str, str, bool]:
    value = _result_value(result)
    # Strings such as "false" are truthy in Python. A QR success flag
    # controls remote outcome interpretation and must be a real JSON boolean.
    if "ok" in value and type(value["ok"]) is not bool:
        raise ValueError("OPTICAL_RESULT_OK_BOOLEAN_REQUIRED")
    # Decoder identity limits are part of the on-wire contract. Reject an
    # invalid producer sequence before the expensive JSON/zlib QR stages.
    sequence_field=value.get("sequence")
    if sequence_field is None or sequence_field == "":
        sequence="-"
    elif type(sequence_field) is str:
        sequence=sequence_field
    else:
        raise ValueError("OPTICAL_SEQUENCE_STRING_REQUIRED")
    if not 1 <= len(sequence) <= 256:
        raise ValueError("OPTICAL_SEQUENCE_TOO_LONG")
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in sequence):
        # A JSON-escaped lone surrogate cannot be faithfully encoded as
        # UTF-8 metadata by downstream QR/result consumers.
        raise ValueError("OPTICAL_SEQUENCE_INVALID_UNICODE")
    wire_value = _compact_transport_value(value)
    raw, _compressed, token = _encode_raw(wire_value)
    size = adaptive_chunk_chars(len(token)) if chunk_chars is None else int(chunk_chars)
    if size < 120:
        raise ValueError("chunk_chars must be >= 120")
    total = max(1, (len(token) + size - 1) // size)
    digest = hashlib.sha256(raw).hexdigest()
    crc = f"{binascii.crc32(raw) & 0xffffffff:08x}"
    data = value.get("data")
    # An uncompactable caller-provided transport_compacted field is ambiguous
    # to older unmarked decoders. Mark these rare packets as literal metadata.
    literal_marker = (
        isinstance(data,dict)
        and "transport_compacted" in data
    )
    return value, raw, token, size, total, digest, crc, sequence, literal_marker


def _packet_json(
    *, sequence: str, index: int, total: int, digest: str, crc: str, piece: str,
    literal_marker: bool = False,
) -> str:
    packet = {
        "p": PROTOCOL, "v": 2, "s": sequence, "i": index, "n": total,
        "h": digest[:16], "c": crc,
        "x": f"{binascii.crc32(piece.encode('ascii')) & 0xffffffff:08x}",
        "d": piece,
    }
    if literal_marker:
        packet["t"] = 0
    rendered = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    # A caller may request an enormous chunk_chars value. Even if raw/result
    # byte budgets pass, the receiver rejects a >64 KiB QR envelope.
    if len(rendered) > MAX_EC2_PACKET_TEXT_CHARS:
        raise ValueError("OPTICAL_PACKET_TEXT_TOO_LARGE")
    if len(rendered.encode("utf-8")) > MAX_EC2_QR_RENDER_BYTES:
        raise ValueError("OPTICAL_QR_CAPACITY_EXCEEDED")
    return rendered


def _preflight_ec2_header_fields(fields: dict[str, Any]) -> None:
    """Bound untrusted EC2H metadata before json.dumps allocates a string.

    A durable receipt can be generated independently from the bounded EC2
    payload encoder. A multi-megabyte command_id or mutation_state must
    not be copied into a huge transient header just to reject QR capacity.
    """
    for field, value in fields.items():
        if value is None or type(value) is bool:
            continue
        if type(value) is str:
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
                raise ValueError("OPTICAL_HEADER_FIELD_INVALID")
            if len(value) > MAX_EC2_QR_RENDER_BYTES:
                raise ValueError("OPTICAL_QR_CAPACITY_EXCEEDED")
        elif type(value) is int:
            # Python 3.11+ limits decimal conversion; reject early on all
            # supported runtimes without serializing arbitrarily large ints.
            if value.bit_length() > MAX_EC2_QR_RENDER_BYTES * 4:
                raise ValueError("OPTICAL_QR_CAPACITY_EXCEEDED")
        else:
            raise ValueError("OPTICAL_HEADER_FIELD_INVALID")


def _header_json(
    value: dict[str, Any], raw: bytes, *, total: int, digest: str,
    broadcast_count: int | None = None,
) -> str:
    data = value.get("data") if isinstance(value.get("data"), dict) else {}
    receipt = data.get("mutation_receipt") if isinstance(data.get("mutation_receipt"), dict) else {}
    header = {
        "p": HEADER_PROTOCOL, "v": 1,
        "s": str(value.get("sequence") or "-"), "f": True,
        "ok": value.get("ok") is True, "rc": value.get("code"),
        "l": len(raw), "n": int(total), "h": digest,
        "m": receipt.get("lifecycle_state") or receipt.get("state"),
        "cid": value.get("command_id"),
    }
    if broadcast_count is not None:
        header["w"] = max(0, min(int(total), int(broadcast_count)))
    _preflight_ec2_header_fields(header)
    rendered = json.dumps(header, ensure_ascii=False, separators=(",", ":"))
    if len(rendered.encode("utf-8")) > MAX_EC2_QR_RENDER_BYTES:
        raise ValueError("OPTICAL_QR_CAPACITY_EXCEEDED")
    return rendered



def encode_durable_result_header(header: dict[str, Any]) -> str:
    """Encode a terminal EC2H receipt without serializing/compressing the payload.

    This is intentionally durable-only: n/w stay zero until the background
    optical bundle knows the real compressed EC2 packet count.
    """
    if not isinstance(header, dict):
        raise ValueError("durable result header object required")
    sequence=header.get("sequence")
    if (type(sequence) is not str or not 1<=len(sequence)<=256 or
            any(0xD800 <= ord(ch) <= 0xDFFF for ch in sequence)):
        raise ValueError("durable result sequence required")
    if header.get("finished") is not True:
        raise ValueError("durable result is not terminal")
    if type(header.get("success")) is not bool:
        raise ValueError("OPTICAL_RESULT_OK_BOOLEAN_REQUIRED")
    payload_length=header.get("payload_length",0)
    if (type(payload_length) is not int or
            not 0<=payload_length<=64*1024*1024):
        raise ValueError("invalid durable result payload length")
    mutation=header.get("mutation",False)
    if type(mutation) is not bool:
        raise ValueError("OPTICAL_MUTATION_BOOLEAN_REQUIRED")
    mutation_state=header.get("mutation_state") if mutation else None
    sha256=header.get("sha256") or ""
    if type(sha256) is not str:
        raise ValueError("OPTICAL_HEADER_FIELD_INVALID")
    fields={
        "p": HEADER_PROTOCOL, "v": 1, "s": sequence, "f": True,
        "ok": header["success"], "rc": header.get("return_code"),
        "l": payload_length, "n": 0, "h": sha256,
        "m": mutation_state, "cid": header.get("command_id"),
        "w": 0, "d": 1,
    }
    _preflight_ec2_header_fields(fields)
    rendered = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    if len(rendered.encode("utf-8")) > MAX_EC2_QR_RENDER_BYTES:
        raise ValueError("OPTICAL_QR_CAPACITY_EXCEEDED")
    return rendered
def encode_result_chunks(
    result: Any,
    *,
    chunk_chars: int | None = None,
    max_packets: int | None = None,
    indexes: list[int] | tuple[int, ...] | None = None,
    prepared: PreparedOpticalResult | None = None,
) -> list[str]:
    """Encode only the EC2 packet window/indexes that are actually needed."""
    # Remote retransmission parameters can be invalid even for an otherwise
    # valid 16 MiB result. Reject malformed index containers before the
    # expensive JSON/zlib preparation stage.
    if indexes is not None and (
            not isinstance(indexes,(list,tuple))
            or len(indexes)>MAX_EC2_PACKETS):
        raise ValueError("OPTICAL_CHUNK_INDEX_REQUEST_TOO_LARGE")
    # Reject malformed indexes before serializing/compressing a large result.
    parsed_indexes = None
    if indexes is not None:
        parsed_indexes = []
        for raw_index in indexes:
            if (type(raw_index) is not int and
                    not (isinstance(raw_index, str) and len(raw_index) <= 32
                         and raw_index.strip().isdecimal())):
                raise ValueError("invalid optical chunk index")
            index = int(raw_index)
            if index < 0:
                raise IndexError("OPTICAL_CHUNK_OUT_OF_RANGE")
            parsed_indexes.append(index)
        if not parsed_indexes:
            raise ValueError("at least one optical chunk index is required")
    if prepared is None:
        prepared = prepare_optical_result(result, chunk_chars=chunk_chars)
    elif not isinstance(prepared, PreparedOpticalResult):
        raise TypeError("prepared optical result required")
    elif chunk_chars is not None and int(chunk_chars) != prepared.chunk_chars:
        raise ValueError("prepared optical chunk size does not match request")
    token = prepared.token
    size = prepared.chunk_chars
    total = prepared.total
    digest = prepared.digest
    crc = prepared.crc
    sequence = prepared.sequence
    literal_marker = prepared.literal_marker
    if indexes is None:
        materialized = total if max_packets is None else max(1, min(total, int(max_packets)))
        selected = list(range(materialized))
    else:
        # Index requests may originate from remote optical retransmissions.
        # Bound the raw input and use O(1) deduplication instead of a growing
        # list membership scan (O(n^2) on long sparse windows).
        selected: list[int] = []
        seen: set[int] = set()
        for index in parsed_indexes:
            if index >= total:
                raise IndexError("OPTICAL_CHUNK_OUT_OF_RANGE")
            if index not in seen:
                seen.add(index)
                selected.append(index)
        if max_packets is not None:
            selected = selected[:max(1, int(max_packets))]
    packets: list[str] = []
    for index in selected:
        piece = token[index * size:(index + 1) * size]
        packets.append(_packet_json(
            sequence=sequence, index=index, total=total,
            digest=digest, crc=crc, piece=piece,
            literal_marker=literal_marker,
        ))
    return packets


def encode_result_header(result: Any, *, chunk_count: int | None = None) -> str:
    value, raw, _token, _size, total, digest, _crc, _sequence, _literal = _transport_components(result)
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
    value, raw, token, size, total, digest, crc, sequence, literal_marker = _transport_components(
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
            literal_marker=literal_marker,
        ))
    return packets, _header_json(
        value, raw, total=total, digest=digest, broadcast_count=materialized
    )


def decode_result_chunks(payloads: Collection[str]) -> dict[str, Any]:
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
        chunk = decode_ec2_packet_json(raw_packet)
        if not isinstance(chunk, dict):
            # A QR envelope is an object; deeply nested JSON arrays are valid
            # JSON but never valid EC2 packets. Fail closed instead of silently
            # accepting them until the generic "no chunks" error.
            raise ValueError("invalid optical packet JSON")
        if chunk.get("p") != PROTOCOL:
            continue
        if not valid_ec2_identity(chunk):
            raise ValueError("invalid optical transfer identity")
        if first is None:
            first = chunk
            total = chunk.get("n")
            if type(total) is not int or total < 1 or total > MAX_EC2_PACKETS:
                raise ValueError("invalid optical packet count")
            identity = (chunk.get("s"), total, chunk.get("h"),
                        chunk.get("c"), chunk.get("e"), chunk.get("t"))
        elif (chunk.get("s"), chunk.get("n"), chunk.get("h"),
              chunk.get("c"), chunk.get("e"), chunk.get("t")) != identity:
            raise ValueError("mixed optical packet set")
        if "t" in chunk and (type(chunk["t"]) is not int or chunk["t"] != 0):
            raise ValueError("invalid optical transport marker")
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
        # Every EC2 producer includes an eight-character lowercase CRC.
        # Missing, truthy non-string, or malformed fields must not bypass the
        # per-chunk integrity gate in direct decode_result_chunks() callers.
        if (type(fragment_crc) is not str
                or len(fragment_crc) != 8
                or any(ch not in "0123456789abcdef" for ch in fragment_crc)
                or fragment_crc != f"{binascii.crc32(data_bytes) & 0xffffffff:08x}"):
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
    # SHA/CRC verify byte integrity, not JSON semantics. A perfectly hashed
    # payload can still contain conflicting "ok" or "mutation_state" keys.
    value = decode_ec2_packet_json(raw)
    if not isinstance(value, dict):
        raise ValueError("optical result object required")
    # Fragment SHA/CRC authenticate the compressed result bytes, but do not
    # cover the EC2 envelope's sequence. A relabeled QR could otherwise be
    # reported as a verified result for a *different* EC1 request.
    # The producer uses "-" only for an omitted, None or empty sequence.
    payload_sequence = value.get("sequence")
    if payload_sequence is None or payload_sequence == "":
        payload_sequence = "-"
    if type(payload_sequence) is not str or payload_sequence != first["s"]:
        raise ValueError("optical result sequence mismatch")
    return _expand_transport_value(
        value, preserve_literal_marker=first.get("t") == 0 and
        type(first.get("t")) is int
    )
