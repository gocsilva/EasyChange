"""Only a trusted local owner may serialize durable sequence claims."""
from __future__ import annotations

import os
import stat

import pytest

from easychange.core.result_store import DurableResultStore


@pytest.mark.skipif(os.name != "posix", reason="POSIX file owner/mode contract")
def test_group_writable_claim_lock_is_rejected_without_creating_receipt(tmp_path):
    store = DurableResultStore(tmp_path / "results")
    lock = store._claim_lock_path
    lock.write_bytes(b"0")
    lock.chmod(0o666)
    try:
        with pytest.raises(RuntimeError, match="RESULT_CLAIM_UNSAFE_LOCK_OWNER"):
            store.claim_running("QUNSAFE001", "a" * 64, ":write", mutation=True)
        assert not store._path("QUNSAFE001").exists()
        assert lock.read_bytes() == b"0"
    finally:
        lock.chmod(0o600)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file owner/mode contract")
def test_private_claim_lock_still_allows_atomic_sequence_idempotency(tmp_path):
    store = DurableResultStore(tmp_path / "results")
    lock = store._claim_lock_path
    lock.write_bytes(b"0")
    lock.chmod(0o600)
    receipt, won = store.claim_running("QSAFE002", "b" * 64,
                                      ":write", mutation=True)
    assert won is True
    replay, won_again = DurableResultStore(store.root).claim_running(
        "QSAFE002", "b" * 64, ":write", mutation=True)
    assert won_again is False
    assert replay["execution_id"] == receipt["execution_id"]
    assert stat.S_ISREG(lock.stat().st_mode)
    assert not lock.stat().st_mode & 0o022


@pytest.mark.skipif(os.name != "posix", reason="POSIX hard-link inode contract")
def test_hardlinked_claim_lock_fails_closed_and_recovers_after_unlink(tmp_path):
    store = DurableResultStore(tmp_path / "results")
    lock = store._claim_lock_path
    lock.write_bytes(b"0")
    lock.chmod(0o600)
    alias = tmp_path / "aliased-owner.lock"
    os.link(lock, alias)
    sequence = "QHARDLINK01"
    try:
        with pytest.raises(RuntimeError, match="RESULT_CLAIM_UNSAFE_LOCK_LINKS"):
            store.claim_running(sequence, "a" * 64, ":mutate", mutation=True)
        assert not store._path(sequence).exists()
        assert lock.read_bytes() == b"0"
    finally:
        alias.unlink()
    claimed, acquired = store.claim_running(
        sequence, "a" * 64, ":mutate", mutation=True)
    assert acquired is True
    assert claimed["state"] == "RUNNING"
    repeated, acquired = DurableResultStore(store.root).claim_running(
        sequence, "a" * 64, ":mutate", mutation=True)
    assert acquired is False
    assert repeated["execution_id"] == claimed["execution_id"]
