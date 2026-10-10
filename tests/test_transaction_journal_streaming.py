"""Active append-v2 transactions must be streamed on restart."""
from __future__ import annotations

from pathlib import Path

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


def test_restart_loads_journal_without_reading_entire_jsonl(tmp_path, monkeypatch):
    source=tmp_path/"sample.txt"
    source.write_text("first\nsecond\nthird\n",encoding="utf-8")
    active=CommandService(Workspace.open(tmp_path))
    try:
        assert active.execute(":begin").ok
        assert active.execute(":replace-line sample.txt 1 FIRST").ok
        assert active.execute(":replace-line sample.txt 2 SECOND").ok
        journal=active.transaction_log_path
        assert journal.is_file()
        original=Path.read_text
        def deny_whole_journal_read(self,*args,**kwargs):
            if self==journal:
                raise AssertionError("JSONL_MUST_BE_STREAMED")
            return original(self,*args,**kwargs)

        with monkeypatch.context() as patcher:
            patcher.setattr(Path,"read_text",deny_whole_journal_read)
            restored=CommandService(Workspace.open(tmp_path))
        try:
            assert restored.transaction is not None
            assert len(restored.transaction)==2
            assert restored._transaction_persisted_count==2
            assert restored.execute(":rollback").ok
        finally:
            restored.close()
        assert source.read_text(encoding="utf-8")=="first\nsecond\nthird\n"
    finally:
        active.close()
