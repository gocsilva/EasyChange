"""Cleanup retention must stay bounded even with many legacy U1 directories."""
from __future__ import annotations

import os
import time

from easychange.core.inbound_transfer import InboundCommandStore


def test_inbound_cleanup_retains_only_newest_transfer_directories(tmp_path):
    store=InboundCommandStore(tmp_path/"uploads")
    store.MAX_TRANSFERS=5
    now=time.time()
    for index in range(14):
        directory=store.root/f"UDEMO{index:04d}"
        directory.mkdir()
        (directory/"meta.json").write_text("evidence",encoding="utf-8")
        timestamp=now-(index*10)
        os.utime(directory,(timestamp,timestamp))
    expired=store.root/"UOLD9999"
    expired.mkdir()
    os.utime(expired,(now-4000,now-4000))

    with store._transaction_lock():
        store._cleanup_locked()
    retained={item.name for item in store.root.iterdir() if item.is_dir()}
    assert retained=={f"UDEMO{index:04d}" for index in range(5)}
    assert all((store.root/name/"meta.json").read_text()=="evidence"
               for name in retained)


def test_cleanup_preserves_recovery_after_disappearing_staging_directory(tmp_path):
    store=InboundCommandStore(tmp_path/"uploads")
    store.MAX_TRANSFERS=2
    now=time.time()
    first=store.root/"UFIRST000"
    first.mkdir()
    os.utime(first,(now-5,now-5))
    second=store.root/"USECOND00"
    second.mkdir()
    os.utime(second,(now-3,now-3))
    third=store.root/"UTHIRD000"
    third.mkdir()
    os.utime(third,(now-1,now-1))
    with store._transaction_lock():
        store._cleanup_locked()
    assert not first.exists()
    assert second.exists() and third.exists()
    # Idempotent maintenance does not remove surviving transfer evidence.
    with store._transaction_lock():
        store._cleanup_locked()
    assert second.exists() and third.exists()


def test_begin_enforces_transfer_count_after_new_reservation(tmp_path):
    """Creating the next transfer must not leave MAX_TRANSFERS+1 directories."""
    import hashlib
    store=InboundCommandStore(tmp_path/"uploads")
    store.MAX_TRANSFERS=3
    for index in range(4):
        store.begin(
            f"UCAPACITY{index:03d}",
            raw_bytes=2,compressed_bytes=2,chunk_count=1,
            sha256=hashlib.sha256(b"ok").hexdigest(),
        )
    names={p.name for p in store.root.iterdir() if p.is_dir()}
    assert len(names)==store.MAX_TRANSFERS
    assert "UCAPACITY000" not in names
    assert "UCAPACITY003" in names
