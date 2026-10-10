"""Bulk read-regions honors the output budget before reading every source."""
from __future__ import annotations

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def _regions(count):
    return [{"path":f"source-{i}.txt","start":1,"count":1}
            for i in range(count)]


def test_read_regions_stops_submitting_new_batches_after_budget(tmp_path, monkeypatch):
    service=CommandService(Workspace.open(tmp_path))
    seen=[]
    try:
        monkeypatch.setattr(service,"_resolve_ref",lambda value:value)
        def read_one(path,start,count):
            seen.append(path)
            return {"path":path,"start":start,"lines":["L"*21000],
                    "total_lines":1,"hash":"abc","has_more":False,
                    "next_start":2}
        monkeypatch.setattr(service.files,"read",read_one)
        result=service._read_regions_data(
            _regions(32), max_bytes=32768, merge_overlaps=False)
        assert result["count"]==1
        assert result["requested"]==32
        assert result["coalesced"]==32
        assert result["truncated"] is True
        assert result["has_more"] is True
        assert result["remaining"]==31
        assert [item["path"] for item in result["skipped"]]==[
            f"source-{i}.txt" for i in range(1,32)]
        # The first worker group may already be running, but all 32 files
        # must not be read just to return the single useful region.
        assert 2<=len(seen)<=12
    finally:
        service.close()


def test_read_regions_preserves_order_when_all_regions_fit(tmp_path, monkeypatch):
    service=CommandService(Workspace.open(tmp_path))
    seen=[]
    try:
        monkeypatch.setattr(service,"_resolve_ref",lambda value:value)
        def read_one(path,start,count):
            seen.append(path)
            return {"path":path,"start":start,"lines":["small"],
                    "total_lines":1,"hash":"xyz","has_more":False,
                    "next_start":2}
        monkeypatch.setattr(service.files,"read",read_one)
        result=service._read_regions_data(
            _regions(3), max_bytes=32768, merge_overlaps=False)
        assert result["count"]==3
        assert [item["path"] for item in result["regions"]]==[
            f"source-{i}.txt" for i in range(3)]
        assert len(seen)==3
        assert result["skipped"]==[]
        assert result["truncated"] is False
        assert result["failed"]==0
    finally:
        service.close()
