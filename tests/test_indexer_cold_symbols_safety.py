"""Cold symbol discovery may never bypass workspace or index byte limits."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from easychange.core.indexer import Indexer, MAX_INDEX_BYTES


def _cold_index(root: Path, monkeypatch) -> Indexer:
    root.mkdir(parents=True, exist_ok=True)
    index=Indexer(root)
    # Keep these focused on the synchronous fast path; no background race.
    monkeypatch.setattr(index, "start_background_refresh", lambda: None)
    return index


def test_cold_symbols_reject_parent_and_absolute_file_paths(tmp_path, monkeypatch):
    root=tmp_path/"project"
    outside=tmp_path/"external.py"
    outside.write_text("def private_definition():\n    pass\n",encoding="utf-8")
    index=_cold_index(root,monkeypatch)
    for path in ("../external.py", "../../external.py", str(outside),
                 r"C:\Windows\secret.py", "folder/../external.py"):
        with pytest.raises(ValueError,match="INVALID_PROTECTED_INDEX_PATH"):
            index.symbols(path=path)
    # Inspect persisted rows without triggering a full index refresh,
    # which would be unrelated to the cold-path safety contract.
    with index._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_cold_symbols_skip_symlink_to_external_source(tmp_path, monkeypatch):
    root=tmp_path/"project"
    outside=tmp_path/"private.py"
    outside.write_text("def credential_symbol():\n    pass\n",encoding="utf-8")
    index=_cold_index(root,monkeypatch)
    alias=root/"alias.py"
    try:
        alias.symlink_to(outside)
    except (OSError,NotImplementedError):
        pytest.skip("symbolic links unavailable in this environment")
    with patch.object(Path,"read_text",
                      side_effect=AssertionError("UNBOUNDED_OR_EXTERNAL_READ")):
        assert index.symbols(path="alias.py") == []
    # Inspect persisted rows without triggering a full index refresh,
    # which would be unrelated to the cold-path safety contract.
    with index._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_cold_symbols_preserve_protected_workspace_secrets(tmp_path, monkeypatch):
    root=tmp_path/"project"
    root.mkdir()
    (root/"secret.py").write_text(
        "def never_index_this_secret():\n    pass\n",encoding="utf-8")
    index=_cold_index(root,monkeypatch)
    index.protect_path("secret.py",scrub=False)
    with patch.object(Path,"read_text",
                      side_effect=AssertionError("PROTECTED_FILE_READ")):
        assert index.symbols(path="secret.py") == []
    # Inspect persisted rows without triggering a full index refresh,
    # which would be unrelated to the cold-path safety contract.
    with index._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_cold_symbols_skip_large_file_without_reading_it(tmp_path, monkeypatch):
    root=tmp_path/"project"
    root.mkdir()
    giant=root/"giant.py"
    giant.write_bytes(b"def large():\n" + b"x"*(MAX_INDEX_BYTES+128))
    index=_cold_index(root,monkeypatch)
    original=Path.open

    def watch(self,*args,**kwargs):
        if self == giant:
            raise AssertionError("LARGE_FILE_READ")
        return original(self,*args,**kwargs)

    with patch.object(Path,"open",watch):
        assert index.symbols(path="giant.py") == []
    # Inspect persisted rows without triggering a full index refresh,
    # which would be unrelated to the cold-path safety contract.
    with index._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_cold_symbols_keep_small_valid_python_functions(tmp_path, monkeypatch):
    root=tmp_path/"project"
    source=root/"src"/"component.py"
    source.parent.mkdir(parents=True)
    source.write_text("def expected_symbol():\n    return 42\n",encoding="utf-8")
    index=_cold_index(root,monkeypatch)
    result=index.symbols(path="src/component.py")
    assert [(item["name"],item["file"]) for item in result] == [
        ("expected_symbol","src/component.py")
    ]
