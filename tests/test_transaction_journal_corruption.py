"""Corrupt active transaction evidence must never become an empty transaction."""
from __future__ import annotations

import json

import pytest

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def _paths(root):
    folder=root/".easychange"
    folder.mkdir(exist_ok=True)
    return folder/"active_transaction.json",folder/"active_transaction.jsonl"


def _record(path="sample.txt"):
    return json.dumps({
        "path":path,"before":"original\n","after":"changed\n",
        "change_id":None,"command":"replace-line",
    },separators=(",",":"))


def test_corrupt_active_transaction_header_fails_closed_and_is_preserved(tmp_path):
    header,log=_paths(tmp_path)
    header.write_text('{"transaction_id":',encoding="utf-8")
    original=header.read_bytes()
    with pytest.raises(RuntimeError,match="ACTIVE_TRANSACTION_RECOVERY_FAILED"):
        CommandService(Workspace.open(tmp_path))
    assert header.read_bytes()==original
    assert not log.exists()

    header.write_text(json.dumps({"format":"append-v2","transaction_id":"TXRESCUED"}),
                      encoding="utf-8")
    repaired=CommandService(Workspace.open(tmp_path))
    try:
        assert repaired.transaction_id=="TXRESCUED"
        assert repaired.transaction==[]
    finally:
        repaired.close()


def test_invalid_middle_journal_line_cannot_hide_later_rollback_record(tmp_path):
    header,log=_paths(tmp_path)
    header.write_text(json.dumps({"format":"append-v2","transaction_id":"TXSAFE"}),
                      encoding="utf-8")
    content=_record()+"\n"+"NOT_JSON\n"+_record("later.txt")+"\n"
    log.write_text(content,encoding="utf-8")
    with pytest.raises(RuntimeError,match="ACTIVE_TRANSACTION_RECOVERY_FAILED"):
        CommandService(Workspace.open(tmp_path))
    assert log.read_text(encoding="utf-8")==content


def test_torn_final_append_is_uncertain_and_must_not_be_skipped(tmp_path):
    header,log=_paths(tmp_path)
    header.write_text(json.dumps({"format":"append-v2","transaction_id":"TXTRUNC"}),
                      encoding="utf-8")
    partial=_record()+"\n"+'{"path":"not-finished"'
    log.write_text(partial,encoding="utf-8")
    for _ in range(2):
        with pytest.raises(RuntimeError,match="ACTIVE_TRANSACTION_RECOVERY_FAILED"):
            CommandService(Workspace.open(tmp_path))
        # Starting the service cannot truncate or silently repair possible
        # rollback evidence, even on repeated initialization attempts.
        assert log.read_text(encoding="utf-8")==partial
        assert json.loads(header.read_text(encoding="utf-8"))["transaction_id"]=="TXTRUNC"
