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
def test_case_variant_protection_is_durable_and_scrubs_legacy_rows(tmp_path):
    root=tmp_path/"project"
    root.mkdir()
    secret=root/"Secrets.py"
    secret.write_text("def HiddenToken():\n    return 'SECRET_NEVER_INDEX'\n",
                      encoding="utf-8")
    visible=root/"public.py"
    visible.write_text("def VisibleToken(): return 'PUBLIC_ONLY'\n",
                       encoding="utf-8")

    first=Indexer(root)
    assert first.refresh(force=True)["files"]==2
    assert first.protect_path("secrets.py",scrub=False)["protected"] is True
    with first._connect() as db:
        assert db.execute("SELECT path FROM files ORDER BY path").fetchall()==[
            ("public.py",)]
        assert db.execute("SELECT path FROM symbols WHERE path=?",
                          ("Secrets.py",)).fetchall()==[]
        if first._fts_enabled:
            assert db.execute("SELECT path FROM files_fts WHERE path=?",
                              ("Secrets.py",)).fetchall()==[]
    assert first.update_path("Secrets.py")["protected"] is True
    assert first.search("SECRET_NEVER_INDEX")==[]
    assert first.symbols(name="HiddenToken")==[]

    # The original capitalization is irrelevant after reopening the DB.
    first.close()
    reopened=Indexer(root)
    assert reopened.protected_paths()==["secrets.py"]
    assert reopened.refresh(force=True)["files"]==1
    assert reopened.update_path("Secrets.py")["protected"] is True
    assert reopened.search("SECRET_NEVER_INDEX")==[]
    assert reopened.symbols(name="HiddenToken")==[]
    assert reopened.search("PUBLIC_ONLY")
    reopened.close()


def test_concurrent_indexers_merge_protected_paths_without_losing_exclusions(tmp_path):
    root=tmp_path/"project"
    root.mkdir()
    for name in ("FirstSecret.py","SecondSecret.py","Visible.py"):
        (root/name).write_text(
            f"def {name[:-3]}Symbol(): return 'SENSITIVE_{name[:-3]}'\n",
            encoding="utf-8")

    a=Indexer(root)
    b=Indexer(root)
    try:
        assert b.refresh(force=True)["files"]==3
        a.protect_path("firstsecret.py",scrub=False)
        # b opened before a protected the file. It must pick up protection
        # from the shared metadata rather than returning stale FTS results.
        assert b.search("SENSITIVE_FirstSecret")==[]
        assert b.symbols(name="FirstSecretSymbol")==[]
        assert b.update_path("FirstSecret.py")["protected"] is True

        b.protect_path("secondsecret.py",scrub=False)
        assert a.search("SENSITIVE_SecondSecret")==[]
        assert a.update_path("SecondSecret.py")["protected"] is True

        # Serialized metadata merge must retain both exclusion records.
        check=Indexer(root)
        try:
            assert set(check.protected_paths())=={
                "firstsecret.py","secondsecret.py"}
            assert check.refresh(force=True)["files"]==1
            assert check.search("SENSITIVE_Visible")
            assert check.search("SENSITIVE_FirstSecret")==[]
            assert check.search("SENSITIVE_SecondSecret")==[]
        finally:
            check.close()
    finally:
        a.close()
        b.close()


def test_protected_legacy_sqlite_rows_cannot_escape_search_or_file_listing(tmp_path):
    root=tmp_path/"project"
    root.mkdir()
    secret=root/"Secrets.py"
    secret.write_text("def SecretSymbol(): return 'HIDDEN_INDEX_MARKER'\n",encoding="utf-8")
    visible=root/"Public.py"
    visible.write_text("def PublicSymbol(): return 'VISIBLE_INDEX_MARKER'\n",encoding="utf-8")
    idx=Indexer(root)
    try:
        assert idx.refresh(force=True)["files"]==2
        idx.protect_path("secrets.py",scrub=False)
        # Simulate a racing/legacy SQLite writer that recreates a now
        # protected row after the cleanup, with matching stat data.
        stat=secret.stat()
        with idx._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO files
                   (path,extension,size,mtime_ns,sha256,language,content)
                   VALUES(?,?,?,?,?,?,?)""",
                ("Secrets.py",".py",stat.st_size,stat.st_mtime_ns,
                 "legacy-hash","python",secret.read_text(encoding="utf-8")),
            )
            db.execute("""INSERT INTO symbols(path,name,kind,line,column_no,
                        signature,file_hash) VALUES(?,?,?,?,?,?,?)""",
                       ("Secrets.py","SecretSymbol","function",1,1,
                        "def SecretSymbol():","legacy-hash"))
            if idx._fts_enabled:
                db.execute("INSERT INTO files_fts(path,content) VALUES(?,?)",
                           ("Secrets.py",secret.read_text(encoding="utf-8")))
        assert idx.search("HIDDEN_INDEX_MARKER")==[]
        assert idx.symbols(name="SecretSymbol")==[]
        listing=idx.files(offset=0,limit=10)
        assert [item["path"] for item in listing]==["Public.py"]
        assert idx.files(offset=0,limit=1)[0]["path"]=="Public.py"
        assert idx.files(offset=1,limit=1)==[]
        assert idx.search("VISIBLE_INDEX_MARKER")
        if idx._fts_enabled:
            # Repeat through the ordinary SQLite fallback instead of FTS.
            idx._fts_enabled=False
            assert idx.search("HIDDEN_INDEX_MARKER")==[]
            idx._fts_enabled=True
    finally:
        idx.close()


def test_corrupt_persisted_protection_metadata_fails_closed(tmp_path):
    root=tmp_path/"project"
    root.mkdir()
    index=Indexer(root)
    index.close()
    with sqlite3.connect(root/".easychange"/"index.db") as db:
        db.execute("INSERT OR REPLACE INTO index_meta(key,value) VALUES(?,?)",
                   ("protected_paths_v1", '{"not":"a list"}'))
    with pytest.raises(RuntimeError,match="INDEXER_PROTECTED_PATHS_CORRUPT"):
        Indexer(root)


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


def test_warm_search_purges_file_replaced_by_external_symlink(tmp_path):
    project=tmp_path/"project"
    project.mkdir()
    indexed=project/"indexed.py"
    indexed.write_text("def EARLIER_SYMBOL(): pass\n",encoding="utf-8")
    index=Indexer(project)
    index.refresh(force=True)
    assert index.search("EARLIER_SYMBOL")
    indexed.unlink()
    outside=tmp_path/"outside.py"
    outside.write_text("def OUTSIDE_SYMBOL(): pass\n",encoding="utf-8")
    try:
        indexed.symlink_to(outside)
    except (OSError,NotImplementedError):
        pytest.skip("symlink unavailable")
    assert index.search("EARLIER_SYMBOL")==[]
    with index._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM files WHERE path='indexed.py'").fetchone()[0]==0
        assert db.execute(
            "SELECT count(*) FROM symbols WHERE path='indexed.py'").fetchone()[0]==0
    # The external file is never indexed by a subsequent full refresh either.
    index.invalidate()
    index.refresh(force=True)
    assert index.search("OUTSIDE_SYMBOL")==[]


def test_hot_symbol_lookup_never_leaks_legacy_rows_from_ignored_folders(tmp_path):
    root=tmp_path/"workspace"
    root.mkdir()
    index=Indexer(root)
    # Simulate a DB produced by an earlier index version before directory
    # exclusions. Readers must fail closed even before the next refresh.
    with index._connect() as db:
        for relative in ("node_modules/cache.py",".venv/lib/private.py",
                         ".easychange/cache.py","src/visible.py"):
            db.execute(
                "INSERT INTO symbols(path,name,kind,line,column_no,signature,file_hash) "
                "VALUES(?,?,?,?,?,?,?)",
                (relative,"same_name","function",1,1,
                 "def same_name(): pass","old"))
    index._fully_indexed=True
    result=index.symbols(name="same_name")
    assert [(row["file"],row["name"]) for row in result]==[
        ("src/visible.py","same_name")]
    for excluded in ("node_modules/cache.py",".venv/lib/private.py",
                     ".easychange/cache.py"):
        assert index.symbols(path=excluded)==[]


def test_symlinked_parent_cannot_read_internal_protected_workspace_folder(
        tmp_path,monkeypatch):
    from subprocess import CompletedProcess
    import easychange.core.indexer as module

    project=tmp_path/"workspace"
    project.mkdir()
    private=project/".easychange"/"private.py"
    private.parent.mkdir()
    private.write_text("def PRIVATE_MARKER(): pass\n",encoding="utf-8")
    alias=project/"source_alias"
    try:
        alias.symlink_to(private.parent,target_is_directory=True)
    except (OSError,NotImplementedError):
        pytest.skip("directory symlinks unavailable")

    index=Indexer(project)
    monkeypatch.setattr(index,"start_background_refresh",lambda:None)
    with pytest.raises(ValueError,match="INVALID_PROTECTED_INDEX_PATH"):
        index.update_path("source_alias/private.py")
    # The cold direct-symbol path must also refuse the ancestor alias.
    assert index.symbols(path="source_alias/private.py")==[]

    (project/".git").mkdir()
    def fake_git_search(argv,**kwargs):
        return CompletedProcess(argv,0,
            "source_alias/private.py:1:def PRIVATE_MARKER(): pass\n","")
    monkeypatch.setattr(module.subprocess,"run",fake_git_search)
    assert index._git_search("PRIVATE_MARKER",regex=False,extension=None,
                             path_prefix=None,case_sensitive=False)==[]
    with index._connect() as db:
        assert db.execute("SELECT count(*) FROM files").fetchone()[0]==0


def test_warm_index_removes_legacy_row_reached_via_directory_symlink(tmp_path):
    project=tmp_path/"workspace"
    project.mkdir()
    private=project/".easychange"/"private.py"
    private.parent.mkdir()
    private.write_text("PRIVATE_MARKER = 'do-not-index'\n",encoding="utf-8")
    alias=project/"source_alias"
    try:
        alias.symlink_to(private.parent,target_is_directory=True)
    except (OSError,NotImplementedError):
        pytest.skip("directory symlinks unavailable")

    index=Indexer(project)
    with index._connect() as db:
        db.execute(
            "INSERT INTO files(path,extension,size,mtime_ns,sha256,language,content) "
            "VALUES(?,?,?,?,?,?,?)",
            ("source_alias/private.py",".py",private.stat().st_size,
             private.stat().st_mtime_ns,"old","python",
             "PRIVATE_MARKER = 'do-not-index'"))
    # Make the code path deterministic regardless of SQLite's FTS extension.
    index._fts_enabled=False
    index._fully_indexed=True
    assert index.search("PRIVATE_MARKER")==[]
    with index._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM files WHERE path=?",
            ("source_alias/private.py",)).fetchone()[0]==0


def test_file_reader_rechecks_symlink_after_workspace_scan(tmp_path):
    project=tmp_path/"project"
    project.mkdir()
    secret=tmp_path/"secret.py"
    secret.write_text("def OUTSIDE_SECRET(): pass\n", encoding="utf-8")
    link=project/"candidate.py"
    try:
        link.symlink_to(secret)
    except (NotImplementedError,OSError):
        pytest.skip("file symlink unavailable")

    metadata=secret.stat()
    record=Indexer._read_candidate(
        (link,"candidate.py",metadata.st_size,metadata.st_mtime_ns),
        trusted_root=project)
    assert record[3]==""
    assert record[4] is None
    assert record[5]==[]


def test_file_reader_rechecks_directory_alias_after_workspace_scan(tmp_path):
    project=tmp_path/"project"
    project.mkdir()
    outside=tmp_path/"outside"
    outside.mkdir()
    private=outside/"secret.py"
    private.write_text("def SECRET(): pass\n", encoding="utf-8")
    alias=project/"source"
    try:
        alias.symlink_to(outside,target_is_directory=True)
    except (NotImplementedError,OSError):
        pytest.skip("directory symlink unavailable")

    stat=private.stat()
    result=Indexer._read_candidate(
        (alias/"secret.py","source/secret.py",stat.st_size,stat.st_mtime_ns),
        trusted_root=project)
    assert result[3]==""
    assert result[4] is None


def test_failed_incremental_read_removes_previous_symbols(tmp_path,monkeypatch):
    project=tmp_path/"workspace"
    project.mkdir()
    file=project/"changing.py"
    file.write_text("def PREVIOUS_SYMBOL(): pass\n", encoding="utf-8")
    index=Indexer(project)
    index.refresh(force=True)
    assert index.symbols(name="PREVIOUS_SYMBOL")

    # The scan observes the new stat, but the file becomes unreadable before
    # the background reader runs. The old SQLite row must be retired.
    file.write_text("def NEW_SYMBOL_WITH_LONGER_NAME(): pass\n",
                    encoding="utf-8")
    def failed_read(item, *, trusted_root=None):
        _path, relative, size, mtime=item
        return relative,size,mtime,"",None,[]
    monkeypatch.setattr(Indexer,"_read_candidate",staticmethod(failed_read))
    index.invalidate()
    report=index.refresh(force=True)
    # A failed or unsafe candidate must not be counted as a successful index.
    assert report["indexed"]==0
    assert index.symbols(name="PREVIOUS_SYMBOL")==[]
    with index._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM files WHERE path='changing.py'"
        ).fetchone()[0]==0


def test_direct_update_failed_read_deletes_stale_row(tmp_path,monkeypatch):
    file=tmp_path/"changing.py"
    file.write_text("def OLD_DIRECT(): pass\n",encoding="utf-8")
    index=Indexer(tmp_path)
    index.refresh(force=True)
    assert index.symbols(name="OLD_DIRECT")

    def skipped(item, *, trusted_root=None):
        _path, rel, size, mtime=item
        return rel,size,mtime,"",None,[]
    monkeypatch.setattr(Indexer,"_read_candidate",staticmethod(skipped))
    result=index.update_path("changing.py")
    assert result["removed"] is True
    assert index.symbols(name="OLD_DIRECT")==[]
