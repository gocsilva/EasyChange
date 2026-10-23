import pytest

from easychange.core.locks import LockManager


@pytest.mark.parametrize("ttl", [0, -1, -3600, True])
def test_lock_rejects_nonpositive_or_boolean_ttl_without_mutating_storage(tmp_path, ttl):
    manager = LockManager(tmp_path / "locks.json")
    with pytest.raises(ValueError, match="INVALID_LOCK_TTL"):
        manager.acquire("safe.txt", "owner", "session", ttl_seconds=ttl)
    assert manager.list() == []


def test_lock_valid_ttl_and_default_keep_exclusivity(tmp_path):
    manager = LockManager(tmp_path / "locks.json")
    lock = manager.acquire("safe.txt", "owner", "session", ttl_seconds=1)
    assert lock.expires_at is not None
    with pytest.raises(BlockingIOError):
        manager.acquire("safe.txt", "other", "session", ttl_seconds=1)


@pytest.mark.parametrize("ttl", ["0", "-1"])
def test_lock_command_rejects_invalid_explicit_ttl(tmp_path, ttl):
    from easychange.core.command_service import CommandService
    from easychange.core.workspace import Workspace

    (tmp_path / "safe.txt").write_text("content", encoding="utf-8")
    service = CommandService(Workspace.open(tmp_path))
    try:
        result = service.execute(f":lock safe.txt {ttl}")
        assert not result.ok
        assert "INVALID_LOCK_TTL" in (result.error or "")
        assert service.locks.list() == []
    finally:
        service.close()
