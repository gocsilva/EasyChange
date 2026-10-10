"""Directory filters must not turn literal SQL LIKE characters into wildcards."""
from __future__ import annotations

import pytest

from easychange.core.indexer import Indexer


@pytest.mark.parametrize("regex", [False, True])
def test_index_search_path_prefix_is_literal_for_fts_and_sqlite(tmp_path, regex):
    special="src_100%!"
    names=(
        f"{special}/exact.py",
        f"{special}/nested/inner.py",
        "srcA100ZZ/unrelated.py",
        f"{special}suffix/sibling.py",
    )
    for relative in names:
        source=tmp_path/relative
        source.parent.mkdir(parents=True,exist_ok=True)
        source.write_text("needle = 1\n",encoding="utf-8")
    index=Indexer(tmp_path)
    index.refresh(force=True)
    original=index._fts_enabled
    for use_fts in (False, True):
        if use_fts and not original:
            continue
        index._fts_enabled=use_fts
        rows=index._candidate_rows(
            "needle",regex=regex,extension=None,path_prefix=special)
        assert {row[0] for row in rows}==set(names[:2])
        # A concrete file prefix may also match that exact path.
        exact=index._candidate_rows(
            "needle",regex=regex,extension=None,path_prefix=names[0])
        assert [row[0] for row in exact]==[names[0]]
    index._fts_enabled=original


@pytest.mark.parametrize("prefix", ["../private", "/tmp/private", "src/../private"])
def test_index_search_does_not_normalize_traversing_filter(tmp_path, prefix):
    (tmp_path/"safe.py").write_text("needle = 1\n",encoding="utf-8")
    index=Indexer(tmp_path)
    index.refresh(force=True)
    with pytest.raises(ValueError,match="INVALID_PROTECTED_INDEX_PATH"):
        index._candidate_rows(
            "needle",regex=False,extension=None,path_prefix=prefix)
