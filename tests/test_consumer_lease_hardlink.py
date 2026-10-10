"""OS consumer lease must not be acquired through a hard-linked inode.

This is a filesystem-only regression test. No remote PC, HID or module
service is accessed.
"""
import os
import pytest
import module_bridge


@pytest.mark.skipif(os.name != "posix", reason="POSIX inode hard-link contract")
def test_hardlinked_consumer_lease_fails_closed_and_recovers(tmp_path, monkeypatch):
    lock = tmp_path / "canonical.lock"
    alias = tmp_path / "second-name.lock"
    lock.write_bytes(b"0")
    os.link(lock, alias)
    assert lock.stat().st_nlink == 2
    monkeypatch.setattr(module_bridge, "_consumer_lease_path", lambda: str(lock))
    with pytest.raises(RuntimeError, match="MCP_CONSUMER_LEASE_UNSAFE_LINKS"):
        module_bridge._acquire_consumer_lease()
    # After the unsafe alias has been removed, the same original inode can
    # be leased normally; refusal must not leak the opened file descriptor.
    alias.unlink()
    lease = module_bridge._acquire_consumer_lease()
    try:
        assert os.fstat(lease.fileno()).st_nlink == 1
    finally:
        module_bridge._release_consumer_lease(lease)
