"""Receipt reads must never block on FIFO paths or follow hard-link aliases."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from easychange.core.result_store import DurableResultStore


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "mkfifo"),
                    reason="FIFO nonblocking receipt guard requires POSIX")
def test_fifo_receipt_is_rejected_before_blocking_open(tmp_path):
    store = DurableResultStore(tmp_path / "receipts")
    target = store._path("QFIFO001")
    os.mkfifo(target)
    original_open = os.open
    observed = []

    def checked_open(path, flags, *args, **kwargs):
        if os.fspath(path) == os.fspath(target):
            # This assertion runs BEFORE opening the FIFO, so even a future
            # regression fails safely rather than hanging the entire CI job.
            assert flags & os.O_NONBLOCK, "FIFO receipt opened in blocking mode"
            observed.append(flags)
        return original_open(path, flags, *args, **kwargs)

    with patch("easychange.core.result_store.os.open", side_effect=checked_open):
        with pytest.raises(RuntimeError, match="RESULT_RECEIPT_UNREADABLE"):
            store.get("QFIFO001")
    assert len(observed) == 1
    assert target.exists()
    with pytest.raises(RuntimeError, match="RESULT_RECEIPT_UNREADABLE"):
        store.claim_running("QFIFO001", "a" * 64, ":write", mutation=True)


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_hardlinked_receipt_cannot_alias_evidence_outside_store(tmp_path):
    store = DurableResultStore(tmp_path / "receipts")
    record = store.mark_running("QHARD001", "b" * 64, ":write", mutation=True)
    target = store._path("QHARD001")
    foreign = tmp_path / "external-evidence"
    before = target.read_bytes()
    try:
        os.link(target, foreign)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"hard links not available: {type(exc).__name__}")

    with pytest.raises(RuntimeError, match="RESULT_RECEIPT_UNREADABLE"):
        store.get("QHARD001")
    assert target.read_bytes() == foreign.read_bytes() == before
    with pytest.raises(RuntimeError, match="RESULT_RECEIPT_UNREADABLE"):
        store.claim_running("QHARD001", "b" * 64, ":write", mutation=True)

    foreign.unlink()
    assert store.get("QHARD001")["execution_id"] == record["execution_id"]


def test_ordinary_receipt_still_replays_after_guard(tmp_path):
    store = DurableResultStore(tmp_path / "receipts")
    first, acquired = store.claim_running("QNORMAL1", "c" * 64, ":read",
                                          mutation=False)
    replay, acquired_again = store.claim_running("QNORMAL1", "c" * 64, ":read",
                                                 mutation=False)
    assert acquired is True
    assert acquired_again is False
    assert replay["execution_id"] == first["execution_id"]
