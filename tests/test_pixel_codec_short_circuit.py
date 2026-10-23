"""ECP1 codec must preserve its bitmap while skipping unused data cells."""
import binascii
import struct

import pytest

from easychange.remote.pixel_transport import (
    DATA_START, HEADER_BYTES, LARGE_SIDE, SIDE,
    decode_pixel_bitmap, encode_pixel_bitmap,
)


class CountingBitmap:
    def __init__(self, values):
        self.values = values
        self.reads = 0

    def __len__(self):
        return len(self.values)

    def __getitem__(self, index):
        self.reads += 1
        return self.values[index]


def original_pixel_wire(text, side):
    """Independent reference for the previous full-capacity encoder."""
    data_side = side - DATA_START * 2
    payload = text.encode("utf-8")
    body = b"ECP1" + struct.pack(">HI", len(payload), binascii.crc32(payload) & 0xFFFFFFFF) + payload
    bitmap = bytearray([255]) * (side * side)
    for y in range(1, side - 1):
        for x in range(1, side - 1):
            if x == 1 or x == side - 2 or y == 1 or y == side - 2:
                bitmap[y * side + x] = 0
    for index in range(data_side * data_side):
        byte_index = index // 8
        if byte_index < len(body) and body[byte_index] & (0x80 >> (index % 8)):
            x, y = DATA_START + index % data_side, DATA_START + index // data_side
            bitmap[y * side + x] = 0
    return bytes(bitmap)


@pytest.mark.parametrize("side", [SIDE, LARGE_SIDE])
@pytest.mark.parametrize("payload", ["x", "sessão:✅", "P" * 250])
def test_optimized_codec_preserves_exact_legacy_wire_and_payload(side, payload):
    bitmap = encode_pixel_bitmap(payload, side=side)
    assert bitmap == original_pixel_wire(payload, side)
    assert decode_pixel_bitmap(bitmap, side=side) == payload


@pytest.mark.parametrize("side", [SIDE, LARGE_SIDE])
def test_bad_magic_rejected_before_reading_full_payload_capacity(side):
    bitmap = bytearray(encode_pixel_bitmap("X", side=side))
    bitmap[DATA_START * side + DATA_START] ^= 255
    counted = CountingBitmap(bitmap)
    assert decode_pixel_bitmap(counted, side=side) is None
    # Ring probes + 10-byte header, rather than 52x52 / 68x68 payload bits.
    assert counted.reads <= 150


@pytest.mark.parametrize("side", [SIDE, LARGE_SIDE])
def test_short_valid_payload_decodes_without_reading_unused_capacity(side):
    counted = CountingBitmap(encode_pixel_bitmap("X", side=side))
    assert decode_pixel_bitmap(counted, side=side) == "X"
    assert counted.reads <= 150
