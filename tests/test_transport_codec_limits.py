"""Compressed HID command preflight must reject oversized tokens before decode."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from easychange.core import transport_codec


def test_z1_large_base64_token_is_rejected_before_allocating_decode_buffer():
    raw_limit=64
    permitted=4*((raw_limit+1024+2)//3)+8
    packet=":z1 1 00000000 "+"A"*(permitted+1)
    with patch.object(transport_codec.base64,"urlsafe_b64decode",
                      side_effect=AssertionError("UNBOUNDED_BASE64_DECODE")) as decode:
        with pytest.raises(ValueError,match="EC1_Z1_COMPRESSED_SIZE_LIMIT"):
            transport_codec.decode_command(packet,max_raw_bytes=raw_limit)
    decode.assert_not_called()


@pytest.mark.parametrize("size,crc", (
    ("9"*10000,"00000000"),
    ("1","f"*10000),
    ("1","not-hex!"),
    ("1","0000000g"),
    ("","00000000"),
    ("1","0"*7),
))
def test_z1_malformed_headers_rejected_before_base64_decode(size,crc):
    packet=":z1 "+size+" "+crc+" AAAA"
    with patch.object(transport_codec.base64,"urlsafe_b64decode",
                      side_effect=AssertionError("UNEXPECTED_BASE64_DECODE")) as decoder:
        with pytest.raises(ValueError,match="INVALID_EC1_Z1_HEADER"):
            transport_codec.decode_command(packet,max_raw_bytes=1024)
    decoder.assert_not_called()


def test_ec1_codec_never_stringifies_nontext_commands_or_packets():
    import pytest

    class SideEffectString:
        def __str__(self):
            raise AssertionError("COERCED_UNTRUSTED_COMMAND")

    for invalid in (None,False,0,[],{},SideEffectString()):
        with pytest.raises(ValueError,match="INVALID_EC1_COMMAND"):
            transport_codec.encode_command(invalid)
        with pytest.raises(ValueError,match="INVALID_EC1_Z1_PACKET"):
            transport_codec.decode_command(invalid)


def test_ec1_encoder_rejects_unpaired_surrogates_before_compression():
    import pytest
    text=":write unsafe.txt "+chr(0xD800)
    with pytest.raises(ValueError,match="EC1_COMMAND_INVALID_UTF8"):
        transport_codec.encode_command(text,threshold_bytes=0)


def test_z1_legitimate_compressed_command_still_round_trips():
    command=":write sample.txt \"" + ("valid text " * 140) + "\""
    packet,metadata=transport_codec.encode_command(command,threshold_bytes=0)
    assert metadata["encoding"]=="zlib+base64url"
    restored,report=transport_codec.decode_command(packet,max_raw_bytes=4096)
    assert restored==command
    assert report["raw_bytes"]==len(command.encode("utf-8"))



def test_ec1_encoder_rejects_payload_above_default_decoder_budget():
    # A highly compressible command previously encoded into a valid-looking
    # :z1 frame that CommandService's default decoder could never accept.
    command = ":write oversized.txt " + ("A" * 262145)
    with pytest.raises(ValueError, match="EC1_COMMAND_SIZE_LIMIT"):
        transport_codec.encode_command(command, threshold_bytes=0)


def test_ec1_encoder_size_guard_counts_utf8_bytes_not_characters():
    # The receiver checks uncompressed byte length, not Unicode code points.
    command = "é" * 131073
    with pytest.raises(ValueError, match="EC1_COMMAND_SIZE_LIMIT"):
        transport_codec.encode_command(command, threshold_bytes=0)
