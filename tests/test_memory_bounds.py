from __future__ import annotations

from easychange.core.command_service import CommandService
from easychange.core.journal import Journal
from easychange.core.result import Result
from easychange.core.workspace import Workspace


def test_journal_keeps_large_snapshots_compact_in_memory_and_materializes_on_demand(tmp_path):
    journal = Journal(tmp_path / ".easychange" / "journal.jsonl", "S1")
    before = "A" * 100_000
    after = "B" * 100_000
    entry = journal.record("write", "big.txt", before, after)

    assert entry.before == before
    assert journal._entries[0].before.startswith("@zblob:")
    assert journal._entries[0].after.startswith("@zblob:")
    assert len(journal._entries[0].before) < 100

    loaded = journal.get(entry.change_id)
    assert loaded.before == before
    assert loaded.after == after

    reopened = Journal(tmp_path / ".easychange" / "journal.jsonl", "S2")
    assert reopened._entries[0].before.startswith("@zblob:")
    assert reopened.get(entry.change_id).after == after


def test_command_history_is_bounded_during_long_session(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        for index in range(250):
            service.execute(f":echo value-{index}")
        assert len(service.history) == 100
    finally:
        service.close()


def test_ec_replay_hot_cache_is_bounded_by_items_and_bytes(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        for index in range(20):
            result = Result(True, "read", data={"payload": "x" * 300_000})
            service._remember_ec_result(f"Q{index:04d}X", result, 300_000)
        assert len(service._ec_result_cache) <= 8
        assert sum(service._ec_result_cache_sizes.values()) <= 8 * 1024 * 1024

        huge = Result(True, "read", data={"payload": "x"})
        service._remember_ec_result("QHUGE1", huge, 9 * 1024 * 1024)
        assert "QHUGE1" not in service._ec_result_cache
    finally:
        service.close()


def test_command_service_does_not_duplicate_full_journal_snapshots(tmp_path):
    service = CommandService(Workspace.open(tmp_path))
    try:
        target = tmp_path / "big.txt"
        target.write_text("A" * 100_000, encoding="utf-8")
        before = target.read_text(encoding="utf-8")
        after = "B" * 100_000
        service._record("big.txt", before, after)
        assert service.journal
        assert service.journal[-1].before is None
        assert service.journal[-1].after is None
        assert service.journal_store._entries[-1].before.startswith("@zblob:")
    finally:
        service.close()
