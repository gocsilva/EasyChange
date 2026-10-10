"""The code index never imports source content from outside its workspace."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from easychange.core.indexer import Indexer


@pytest.mark.parametrize("name", [
    "../private.py", "nested/../../private.py", "/tmp/outside.py",
    r"C:\Users\someone\secret.cs", "nested/../secret.py",
    "./", "a//b.py",
])
def test_update_path_rejects_absolute_or_traversing_names(tmp_path, name):
    project=tmp_path/"project"
    project.mkdir()
    external=tmp_path/"private.py"
    external.write_text("PASSWORD_DONT_INDEX=hidden\n",encoding="utf-8")
    index=Indexer(project)
    with pytest.raises(ValueError,match="INVALID_PROTECTED_INDEX_PATH"):
        index.update_path(name)
    with index._connect() as db:
        assert db.execute("SELECT count(*) FROM files").fetchone()[0]==0


def test_direct_update_path_rejects_symlink_to_external_file(tmp_path):
    project=tmp_path/"project"
    project.mkdir()
    outside=tmp_path/"outside.py"
    outside.write_text("def external_secret(): pass\n",encoding="utf-8")
    linked=project/"visible.py"
    try:
        linked.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlink unavailable")
    index=Indexer(project)
    with pytest.raises(ValueError,match="INVALID_PROTECTED_INDEX_PATH"):
        index.update_path("visible.py")
    with index._connect() as db:
        assert db.execute("SELECT count(*) FROM files").fetchone()[0]==0


def test_refresh_ignores_external_and_internal_symlinks(tmp_path):
    project=tmp_path/"project"
    project.mkdir()
    outside=tmp_path/"private.py"
    outside.write_text("def external_secret(): pass\n",encoding="utf-8")
    good=project/"public.py"
    good.write_text("def public_symbol(): pass\n",encoding="utf-8")
    try:
        (project/"outside_link.py").symlink_to(outside)
        (project/"inside_link.py").symlink_to(good)
    except (NotImplementedError, OSError):
        pytest.skip("symlink unavailable")
    index=Indexer(project)
    result=index.refresh(force=True)
    assert result["files"]==1
    with index._connect() as db:
        paths=[row[0] for row in db.execute("SELECT path FROM files").fetchall()]
    assert paths==["public.py"]
    assert index.update_path("public.py")["updated"] is True


def test_refresh_preserves_working_index_and_removes_stale_linked_entries(tmp_path):
    project=tmp_path/"project"
    project.mkdir()
    original=project/"some.py"
    original.write_text("def old_symbol(): pass\n",encoding="utf-8")
    index=Indexer(project)
    index.refresh(force=True)
    assert index.count()==1
    original.unlink()
    outside=tmp_path/"outside.py"
    outside.write_text("def outside_symbol(): pass\n",encoding="utf-8")
    try:
        original.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlink unavailable")
    index.invalidate()
    result=index.refresh()
    assert result["removed"]==1
    assert index.count()==0
    with pytest.raises(ValueError,match="INVALID_PROTECTED_INDEX_PATH"):
        index.update_path("some.py")
