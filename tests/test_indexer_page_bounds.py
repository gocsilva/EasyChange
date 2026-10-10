"""Index pagination must never map a malformed limit to unlimited SQLite."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from easychange.core.indexer import Indexer, MAX_INDEX_PAGE_ROWS


@pytest.mark.parametrize("offset,limit", (
    (-1, 100),
    (True, 100),
    (False, 100),
    (2**63, 100),
    (0, -1),
    (0, True),
    (0, False),
    (0, 100_001),
    (0, 1.5),
    ("0", 100),
))
def test_file_paging_rejects_invalid_values_before_refresh_or_query(
        tmp_path, offset, limit):
    index = Indexer(tmp_path)
    try:
        with patch.object(index, "refresh",
                          side_effect=AssertionError("INDEX_REFRESH_UNNECESSARY")) as refresh, \
             patch.object(index, "_connect",
                          side_effect=AssertionError("SQL_QUERY_UNNECESSARY")) as sql:
            with pytest.raises(ValueError, match="INVALID_INDEX_PAGE"):
                index.files(offset=offset, limit=limit)
        refresh.assert_not_called()
        sql.assert_not_called()
    finally:
        index.close()


@pytest.mark.parametrize("offset,limit", ((-1, 100), (0, -1),
                                         (0, MAX_INDEX_PAGE_ROWS + 1)))
def test_search_rejects_invalid_paging_without_git_or_sql(
        tmp_path, offset, limit):
    index = Indexer(tmp_path)
    try:
        with patch.object(index, "_sync_protected_paths",
                          side_effect=AssertionError("NO_PATH_TRAVERSAL")) as paths, \
             patch.object(index, "_git_search",
                          side_effect=AssertionError("NO_GIT_PROCESS")) as git, \
             patch.object(index, "_candidate_rows",
                          side_effect=AssertionError("NO_SQL_SCAN")) as sql:
            with pytest.raises(ValueError, match="INVALID_INDEX_PAGE"):
                index.search("sensitive", regex=True, offset=offset, limit=limit)
        paths.assert_not_called()
        git.assert_not_called()
        sql.assert_not_called()
    finally:
        index.close()


def test_legacy_100k_rename_window_and_empty_page_remain_supported(tmp_path):
    index = Indexer(tmp_path)
    try:
        index._fully_indexed = True
        assert index.files(limit=MAX_INDEX_PAGE_ROWS) == []
        assert index.files(limit=0) == []
    finally:
        index.close()
