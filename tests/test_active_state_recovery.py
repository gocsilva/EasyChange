from __future__ import annotations

import shutil

from easychange.core.locks import LockManager
from easychange.core.state import MachineSession


def test_machine_session_save_recovers_if_parent_directory_disappears(tmp_path):
    root = tmp_path / ".easychange"
    session = MachineSession(root / "session.json", str(tmp_path))
    session.data["current_file"] = "x.cs"

    shutil.rmtree(root)
    session.save()

    assert (root / "session.json").exists()
    loaded = MachineSession(root / "session.json", str(tmp_path))
    assert loaded.data["current_file"] == "x.cs"


def test_lock_manager_recreates_storage_after_parent_directory_disappears(tmp_path):
    root = tmp_path / ".easychange"
    manager = LockManager(root / "locks.json")
    lock = manager.acquire("a.cs", "owner", "session")
    assert lock.path == "a.cs"

    shutil.rmtree(root)
    # A disappearing EasyChange directory must not crash an active service.
    # The lock database is reconstructed empty and remains usable.
    assert manager.release_owner("owner", "session") == 0
    reacquired = manager.acquire("b.cs", "owner", "session")
    assert reacquired.path == "b.cs"
