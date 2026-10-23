import os
import time

from easychange.core.inbound_transfer import InboundCommandStore


def test_inbound_cleanup_never_deletes_unrelated_stale_directories(tmp_path):
    root = tmp_path / "inbound"
    store = InboundCommandStore(root)
    unrelated = root / "user-notes"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("do not delete", encoding="utf-8")
    old = time.time() - store.TTL_SECONDS - 60
    os.utime(unrelated, (old, old))

    with store._transaction_lock():
        store._cleanup_locked()

    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "do not delete"


def test_inbound_cleanup_still_deletes_expired_valid_transfer_directory(tmp_path):
    root = tmp_path / "inbound"
    store = InboundCommandStore(root)
    transfer = root / "U1234567"
    transfer.mkdir()
    (transfer / "0000.part").write_bytes(b"expired")
    old = time.time() - store.TTL_SECONDS - 60
    os.utime(transfer, (old, old))

    with store._transaction_lock():
        store._cleanup_locked()

    assert not transfer.exists()
