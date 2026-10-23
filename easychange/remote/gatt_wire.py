"""Length-framed messages over 20-160 byte BLE/serial relay packets."""
from __future__ import annotations
import json
import struct

MAX_MESSAGE = 1024 * 1024
MAX_BUFFER = MAX_MESSAGE + 4
CHUNK_BYTES = 160


def _unique_object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("GATT_DUPLICATE_JSON_FIELD")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("GATT_NONFINITE_JSON_NUMBER")


def _strict_json_loads(data: bytes):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object_pairs,
                          parse_constant=_reject_json_constant)
    except RecursionError as exc:
        raise ValueError("GATT_JSON_NESTING_LIMIT") from exc


def frame_object(item: dict) -> bytes:
    raw = json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not 0 < len(raw) <= MAX_MESSAGE:
        raise ValueError("GATT_MESSAGE_TOO_LARGE")
    return struct.pack(">I", len(raw)) + raw

def fragments(item: bytes, chunk_bytes: int = CHUNK_BYTES):
    if not 1 <= chunk_bytes <= 160:
        raise ValueError("GATT_INVALID_CHUNK_SIZE")
    if len(item) > MAX_BUFFER:
        raise ValueError("GATT_FRAME_TOO_LARGE")
    for index in range(0, len(item), chunk_bytes):
        yield item[index:index + chunk_bytes]

class FramedMessages:
    def __init__(self) -> None:
        self._pending = bytearray()
    def reset(self) -> None:
        self._pending.clear()
    def feed(self, payload: bytes) -> list[dict]:
        if not isinstance(payload, bytes) or len(payload) > CHUNK_BYTES:
            raise ValueError("GATT_INVALID_FRAGMENT")
        self._pending.extend(payload)
        # A legal fragment can finish a maximum-sized frame AND start the
        # next one. Drain complete frames before enforcing pending capacity.
        results = []
        while len(self._pending) >= 4:
            count = int.from_bytes(self._pending[:4], "big")
            if count <= 0 or count > MAX_MESSAGE:
                self.reset()
                raise ValueError("GATT_INVALID_FRAME_LENGTH")
            if len(self._pending) < count + 4:
                break
            data = bytes(self._pending[4:4 + count])
            del self._pending[:4 + count]
            try:
                result = _strict_json_loads(data)
            except (ValueError, UnicodeError) as exc:
                # Do not retain a later frame behind a rejected frame: a
                # subsequent feed must never replay stale queued commands.
                self.reset()
                raise ValueError("GATT_INVALID_FRAME_JSON") from exc
            if not isinstance(result, dict):
                self.reset()
                raise ValueError("GATT_FRAME_NOT_OBJECT")
            results.append(result)
        if len(self._pending) > MAX_BUFFER:
            self.reset()
            raise ValueError("GATT_MESSAGE_TOO_LARGE")
        return results


def seal_json(session, data: dict) -> dict:
    """Compress repetitive source before authenticated encryption."""
    import base64
    import zlib
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_MESSAGE:
        raise ValueError("GATT_UNCOMPRESSED_TOO_LARGE")
    payload = (b"Z" + zlib.compress(raw, 3)) if len(raw) > 256 else b"J" + raw
    return {"t": "sealed", "v": base64.b64encode(session.encrypt(payload)).decode("ascii")}

def open_sealed(session, message: dict) -> dict:
    """Authenticate before bounded decompression, never accept zip bombs."""
    import base64
    import zlib
    ciphertext = base64.b64decode(str(message.get("v") or ""), validate=True)
    packed = session.decrypt(ciphertext)
    if not packed:
        raise ValueError("GATT_EMPTY_SEALED_PAYLOAD")
    if packed[:1] == b"Z":
        decompressor = zlib.decompressobj()
        clear = decompressor.decompress(packed[1:], MAX_MESSAGE + 1)
        if len(clear) > MAX_MESSAGE or decompressor.unconsumed_tail or not decompressor.eof or decompressor.unused_data:
            raise ValueError("GATT_DECOMPRESSION_BOUNDS")
    elif packed[:1] == b"J":
        clear = packed[1:]
        if len(clear) > MAX_MESSAGE:
            raise ValueError("GATT_MESSAGE_TOO_LARGE")
    else:
        raise ValueError("GATT_UNSUPPORTED_ENCODING")
    decoded = _strict_json_loads(clear)
    if not isinstance(decoded, dict):
        raise ValueError("GATT_SEALED_NOT_OBJECT")
    return decoded
