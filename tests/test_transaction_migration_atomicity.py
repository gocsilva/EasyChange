"""Legacy -> append-v2 migration must keep rollback evidence crash-safe."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def _legacy(tmp_path):
    folder=tmp_path/".easychange"
    folder.mkdir(exist_ok=True)
    header=folder/"active_transaction.json"
    history=[
        {"path":"one.txt","before":"old-1","after":"new-1",
         "change_id":None,"command":"replace"},
        {"path":"two.txt","before":"old-2","after":"new-2",
         "change_id":None,"command":"replace"},
    ]
    header.write_text(json.dumps({
        "transaction_id":"TXMIGRATE","changes":history,
    }),encoding="utf-8")
    return header,folder/"active_transaction.jsonl",history


def test_legacy_transaction_migration_writes_complete_log_before_header(tmp_path):
    header,log,history=_legacy(tmp_path)
    service=CommandService(Workspace.open(tmp_path))
    try:
        assert service._transaction_storage_v2 is False
        service._save_transaction()
        assert json.loads(header.read_text(encoding="utf-8"))=={
            "format":"append-v2","transaction_id":"TXMIGRATE"}
        assert [json.loads(line) for line in log.read_text(
            encoding="utf-8").splitlines()]==history
        assert service._transaction_persisted_count==len(history)
        service._save_transaction()
        assert len(log.read_text(encoding="utf-8").splitlines())==2
    finally:
        service.close()


def test_failed_header_publication_retains_complete_legacy_rollback(tmp_path,monkeypatch):
    import easychange.core.command_service as command_module

    header,log,history=_legacy(tmp_path)
    original=header.read_bytes()
    service=CommandService(Workspace.open(tmp_path))
    real_replace=os.replace
    def stop_between_log_and_header(source,destination):
        if Path(destination)==header:
            raise OSError("SIMULATED_HEADER_PUBLICATION_FAILURE")
        return real_replace(source,destination)

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(command_module.os,"replace",stop_between_log_and_header)
            with pytest.raises(OSError,match="SIMULATED_HEADER_PUBLICATION_FAILURE"):
                service._save_transaction()
        # The original full history stays authoritative, even though the
        # complete new log was already staged and published safely.
        assert header.read_bytes()==original
        assert [json.loads(line) for line in log.read_text(
            encoding="utf-8").splitlines()]==history
        assert not list(header.parent.glob("active_transaction*.tmp"))

        recovered=CommandService(Workspace.open(tmp_path))
        try:
            assert recovered.transaction_id=="TXMIGRATE"
            assert recovered._transaction_storage_v2 is False
            assert len(recovered.transaction)==2
            assert [change.path for change in recovered.transaction]==[
                "one.txt","two.txt"]
        finally:
            recovered.close()
    finally:
        service.close()
