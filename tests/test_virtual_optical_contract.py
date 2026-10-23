"""Strict public optical wire-contract regression tests."""
import base64
import binascii
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from virtual_optical_contract import decode_lines,focus_valid


def packets(seq="QLAB0001",width=6):
    raw=f'EC1 {seq} OK C1 state 0ms {{"state":"READY"}}'.encode()
    wire=base64.b32encode(raw).decode().rstrip("=")
    chunks=[wire[i:i+width] for i in range(0,len(wire),width)]
    result=[f"ECO1 UBUNTU {seq} OK {len(chunks)} B32"]
    result.extend(f"ECO1 {i:02d} {chunk}" for i,chunk in enumerate(chunks))
    result.append(f"ECO1 END {seq} {binascii.crc32(raw)&0xffffffff:08X}")
    return result


def test_packet_crc_and_indexes_above_nine():
    lines=packets()
    assert len(lines)>12
    assert decode_lines(lines,"QLAB0001")["data"]["state"]=="READY"


def test_corruption_is_not_accepted():
    lines=packets()
    lines[7]=lines[7][:-1]+("A" if lines[7][-1]!="A" else "B")
    assert decode_lines(lines,"QLAB0001") is None


def test_wrong_sequence_not_accepted():
    assert decode_lines(packets(),"QLAB0042") is None


def test_missing_part_duplicate_part_and_checksum_rejected():
    lines=packets()
    assert decode_lines(lines[:4]+lines[5:],"QLAB0001") is None
    assert decode_lines(lines[:3]+[lines[2]]+lines[3:],"QLAB0001") is None
    lines=packets()
    lines[-1]=lines[-1][:-8]+"DEADBEEF"
    assert decode_lines(lines,"QLAB0001") is None


def test_live_status_required():
    assert focus_valid("EasyChang|MACHINE|HID/COMPAC|FOCUS:OMMAND")
    assert not focus_valid("EasyChange|MACHINE|HID/COMPACT|FOCUS: OTHER")
    assert not focus_valid("Microsoft Edge|MACHINE|HID/COMPACT|FOCUS: COMMAND")
    assert not focus_valid("EasyChange|MACHINE|HID/COMPACT|FOCUS: OTHER\nFOCUS: COMMAND")
