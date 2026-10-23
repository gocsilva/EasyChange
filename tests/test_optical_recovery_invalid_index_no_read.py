"""Impossible EC2 recovery indexes must fail without durable receipt I/O."""
import pytest

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace
from easychange.remote.optical_protocol import MAX_EC2_PACKETS


@pytest.mark.parametrize("kind,field,bad", [
    ("optical_chunk", "chunk_index", -1),
    ("optical_chunk", "chunk_index", MAX_EC2_PACKETS),
    ("optical_chunk", "chunk_index", 10**9),
    ("optical_chunks", "chunk_indexes", [-1]),
    ("optical_chunks", "chunk_indexes", [0, MAX_EC2_PACKETS]),
    ("optical_chunks", "chunk_indexes", [10**9]),
])
def test_invalid_index_skips_durable_receipt(tmp_path, monkeypatch, kind, field, bad):
    service = CommandService(Workspace.open(tmp_path))
    reads = []

    def forbidden(*args, **kwargs):
        reads.append(True)
        raise AssertionError("UNNECESSARY_RESULT_READ")

    monkeypatch.setattr(service.result_store, "result", forbidden)
    try:
        reply = service._execute_structured({
            "op": "operation", "type": kind,
            "sequence": "QINVALIDREC", field: bad,
        })
        assert reply.ok is False
        assert reply.code == "INVALID_RESULT_CHUNK"
        assert reads == []
    finally:
        service.close()


@pytest.mark.parametrize("kind,field,index", [
    ("optical_chunk", "chunk_index", "0"),
    ("optical_chunks", "chunk_indexes", [0, " 1 "]),
])
def test_valid_indexes_still_load_receipt(tmp_path, monkeypatch, kind, field, index):
    service = CommandService(Workspace.open(tmp_path))
    reads = []
    monkeypatch.setattr(
        service.result_store, "result",
        lambda sequence: reads.append(sequence) or None,
    )
    try:
        reply = service._execute_structured({
            "op": "operation", "type": kind,
            "sequence": "QVALIDREC", field: index,
        })
        assert reply.code == "RESULT_NOT_FOUND"
        assert reads == ["QVALIDREC"]
    finally:
        service.close()
