"""Post-write integrity failures must never erase another writer's data."""
import pytest

from easychange.core.file_service import FileService
from easychange.core.workspace import Workspace


@pytest.mark.parametrize("created", [False, True])
def test_write_hash_mismatch_does_not_rollback_concurrent_external_update(
    tmp_path, monkeypatch, created
):
    target = tmp_path / "source.cs"
    if not created:
        target.write_bytes(b"original\n")
    service = FileService(Workspace(tmp_path, tmp_path.name))
    original_atomic_write = FileService._atomic_write_bytes
    writes = []

    def concurrent_update(path, payload):
        writes.append(payload)
        original_atomic_write(path, payload)
        if len(writes) == 1:
            # A second process replaces the file after EasyChange's atomic
            # replace but before the post-write integrity check.
            path.write_bytes(b"external editor's latest changes\n")

    monkeypatch.setattr(FileService, "_atomic_write_bytes", staticmethod(concurrent_update))
    with pytest.raises(IOError, match="POST_WRITE_HASH_MISMATCH"):
        service.write("source.cs", "easychange update\n")
    assert target.read_bytes() == b"external editor's latest changes\n"
    assert len(writes) == 1
