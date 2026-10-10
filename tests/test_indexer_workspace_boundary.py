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


def test_git_cold_search_never_exposes_protected_files(tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    import easychange.core.indexer as module

    project=tmp_path/"project"
    project.mkdir()
    (project/".git").mkdir()
    (project/"visible.py").write_text("TOKEN=public\n",encoding="utf-8")
    (project/"private.py").write_text("TOKEN=secret\n",encoding="utf-8")
    index=Indexer(project)
    index.protect_path("private.py",scrub=False)
    calls=[]

    def git_grep(argv, **kwargs):
        calls.append(argv)
        return CompletedProcess(
            args=argv,returncode=0,
            stdout="private.py:1:TOKEN=secret\nvisible.py:1:TOKEN=public\n",
            stderr="")

    monkeypatch.setattr(module.subprocess,"run",git_grep)
    monkeypatch.setattr(index,"start_background_refresh",lambda:None)
    matches=index.search("TOKEN")
    assert [(item["file"],item["text"]) for item in matches]==[
        ("visible.py","TOKEN=public")]
    assert len(calls)==1
    with index._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM files WHERE path='private.py'").fetchone()[0]==0


def test_protection_is_enforced_on_warm_git_fallback(tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    import easychange.core.indexer as module

    project=tmp_path/"project"
    project.mkdir()
    (project/".git").mkdir()
    protected=project/"config.py"
    protected.write_text("SECRET_NEEDLE=hidden\n",encoding="utf-8")
    index=Indexer(project)
    index.refresh(force=True)
    index.protect_path("config.py",scrub=False)

    def git_grep(argv, **kwargs):
        return CompletedProcess(
            args=argv,returncode=0,
            stdout="config.py:1:SECRET_NEEDLE=hidden\n",stderr="")

    monkeypatch.setattr(module.subprocess,"run",git_grep)
    assert index.search("SECRET_NEEDLE")==[]
    with index._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM files WHERE path='config.py'").fetchone()[0]==0


def test_git_search_filters_external_symlink_and_internal_generated_dirs(tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    import easychange.core.indexer as module

    project=tmp_path/"project"
    project.mkdir()
    (project/".git").mkdir()
    (project/"normal.py").write_text("TOKEN=valid\n",encoding="utf-8")
    (project/".easychange").mkdir()
    outside=tmp_path/"out.py"
    outside.write_text("TOKEN=external\n",encoding="utf-8")
    try:
        (project/"linked.py").symlink_to(outside)
    except (OSError,NotImplementedError):
        pytest.skip("symlinks unavailable")
    index=Indexer(project)

    def git_grep(argv, **kwargs):
        return CompletedProcess(args=argv,returncode=0,
            stdout="linked.py:1:TOKEN=external\n.easychange/cache.py:1:TOKEN=internal\n"
                   "normal.py:1:TOKEN=valid\n",stderr="")

    monkeypatch.setattr(module.subprocess,"run",git_grep)
    assert index._git_search(
        "TOKEN",regex=False,extension=None,path_prefix=None,
        case_sensitive=False)==[("normal.py","1:TOKEN=valid")]


def test_update_path_rejects_workspace_internal_symlink_consistently(tmp_path):
    project=tmp_path/"project"
    project.mkdir()
    real=project/"real.py"
    real.write_text("def safe_function(): pass\n",encoding="utf-8")
    alias=project/"alias.py"
    try:
        alias.symlink_to(real)
    except (OSError,NotImplementedError):
        pytest.skip("symlink unavailable")
    index=Indexer(project)
    with pytest.raises(ValueError,match="INVALID_PROTECTED_INDEX_PATH"):
        index.update_path("alias.py")
    index.refresh(force=True)
    with index._connect() as db:
        files=[row[0] for row in db.execute("SELECT path FROM files").fetchall()]
    assert files==["real.py"]


def test_update_path_purges_legacy_index_row_in_ignored_directory(tmp_path):
    project=tmp_path/"project"
    hidden=project/"node_modules"
    hidden.mkdir(parents=True)
    file=hidden/"cached.py"
    file.write_text("TOKEN_THAT_MUST_NOT_BE_INDEXED=1\n",encoding="utf-8")
    index=Indexer(project)
    with index._connect() as db:
        db.execute(
            "INSERT INTO files(path,extension,size,mtime_ns,sha256,language,content) "
            "VALUES(?,?,?,?,?,?,?)",
            ("node_modules/cached.py",".py",file.stat().st_size,1,"old",
             "python","TOKEN_THAT_MUST_NOT_BE_INDEXED=1"))
        db.execute(
            "INSERT INTO symbols(path,name,kind,line,column_no,signature,file_hash) "
            "VALUES(?,?,?,?,?,?,?)",
            ("node_modules/cached.py","TOKEN_THAT_MUST_NOT_BE_INDEXED",
             "function",1,1,"old","old"))
    status=index.update_path("node_modules/cached.py")
    assert status["ignored"] is True
    with index._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM files WHERE path='node_modules/cached.py'"
        ).fetchone()[0]==0
        assert db.execute(
            "SELECT count(*) FROM symbols WHERE path='node_modules/cached.py'"
        ).fetchone()[0]==0
