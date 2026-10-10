"""Inbound U1 wire parameters must not coerce arbitrary Python objects."""
from __future__ import annotations

import base64
import binascii
import hashlib
import zlib

import pytest

from easychange.core.inbound_transfer import InboundCommandStore, InboundTransferError


class UntrustedPrintable:
    def __str__(self):
        raise AssertionError("UNTRUSTED_OBJECT_STRINGIFIED")


def test_inbound_metadata_rejects_nonstrings_before_stringification(tmp_path):
    store=InboundCommandStore(tmp_path/"inbound")
    for value in (UntrustedPrintable(), ["a"*100000], {"sha":"f"*64}, 1, True):
        with pytest.raises(InboundTransferError) as error:
            store.begin("U12345678",raw_bytes=1,compressed_bytes=9,
                        chunk_count=1,sha256=value)
        assert error.value.code=="INVALID_INBOUND_SHA256"


def test_inbound_chunk_rejects_nonstrings_before_stringification_or_disk_io(tmp_path):
    store=InboundCommandStore(tmp_path/"inbound")
    for value in (UntrustedPrintable(), ["A"*100000], {"token":"AA"}, 42, True):
        with pytest.raises(InboundTransferError) as error:
            store.put_chunk("U12345678",index=0,crc32="12345678",token=value)
        assert error.value.code=="INVALID_INBOUND_CHUNK_BASE64URL"
        with pytest.raises(InboundTransferError) as error:
            store.put_chunk("U12345678",index=0,crc32=value,token="AA")
        assert error.value.code=="INVALID_INBOUND_CHUNK_CRC"


def test_inbound_real_string_crc_and_base64_still_verify(tmp_path):
    store=InboundCommandStore(tmp_path/"inbound")
    command=b":read example.md"
    compressed=zlib.compress(command)
    store.begin("U12345678",raw_bytes=len(command),
                compressed_bytes=len(compressed),chunk_count=1,
                sha256=hashlib.sha256(command).hexdigest())
    receipt=store.put_chunk(
        "U12345678",index=0,
        crc32=f"{binascii.crc32(compressed) & 0xffffffff:08x}",
        token=base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("="))
    assert receipt["duplicate"] is False
    restored,proof=store.assemble("U12345678")
    assert restored==command.decode("utf-8")
    assert proof["verified"] is True
