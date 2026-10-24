"""XZ EC2 optical framing: verified bulk savings without weakening CRC."""
from __future__ import annotations
import json
from pathlib import Path
import pytest
from easychange.core.workspace import Workspace
from easychange.core.command_service import CommandService
from easychange.remote.optical_protocol import (
    prepare_optical_result, encode_result_bundle, encode_result_chunks,
    decode_result_chunks, valid_ec2_identity,
)


def test_large_read_xz_fits_original_broadcast_and_roundtrips(tmp_path: Path):
    (tmp_path/"large.txt").write_text(
        "".join(f"{i:04d} Simulation with utf8 action {i}\\n"
                for i in range(900)).replace("\\n","\n"),encoding="utf8")
    service=CommandService(Workspace.open(tmp_path),defer_initial_maintenance=True)
    try:
        result=service.execute(":read large.txt 1 900")
        assert result.ok
        result.sequence="QLABXZ900"
        packets,header=encode_result_bundle(
            result,max_packets=31,overflow_packets=3)
        fragments=[json.loads(packet) for packet in packets]
        metadata=json.loads(header)
        assert fragments[0]["e"]=="xz"
        assert len(packets)<=31
        assert metadata["w"]==metadata["n"]==len(packets)
        decoded=decode_result_chunks(packets)
        assert decoded["sequence"]=="QLABXZ900"
        assert decoded["data"]["returned_lines"]==900
        assert decoded["data"]["lines"]==result.data["lines"]
        prepared=prepare_optical_result(result)
        assert prepared.encoding=="xz"
        subset=encode_result_chunks(
            result,indexes=[0,len(packets)-1],prepared=prepared)
        assert [json.loads(z)["d"] for z in subset] == [
            fragments[0]["d"],fragments[-1]["d"]]
        broken=json.loads(packets[2])
        broken["d"]="X"+broken["d"][1:]
        invalid=list(packets)
        invalid[2]=json.dumps(broken)
        with pytest.raises(ValueError):
            decode_result_chunks(invalid)
        unsupported=json.loads(packets[0])
        unsupported["e"]="unsafe"
        assert not valid_ec2_identity(unsupported)
    finally:
        service.close()


def test_small_result_stays_original_zlib_protocol(tmp_path: Path):
    service=CommandService(Workspace.open(tmp_path),defer_initial_maintenance=True)
    try:
        value=service.execute(":state --compact")
        value.sequence="QLABZLIB01"
        packets,header=encode_result_bundle(value,max_packets=31)
        assert all("e" not in json.loads(piece) for piece in packets)
        assert decode_result_chunks(packets)["ok"] is True
    finally:
        service.close()
