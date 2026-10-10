"""Invalid regex searches must fail before Git/SQLite/index refresh work."""
from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from easychange.core.indexer import Indexer


def test_invalid_regex_cannot_start_git_scan_or_materialize_database_rows(tmp_path):
    index = Indexer(tmp_path)
    try:
        with patch.object(index, "_git_search",
                          side_effect=AssertionError("GIT_SEARCH_SHOULD_NOT_RUN")) as git, \
             patch.object(index, "refresh",
                          side_effect=AssertionError("FULL_REFRESH_SHOULD_NOT_RUN")) as refresh, \
             patch.object(index, "_candidate_rows",
                          side_effect=AssertionError("SQL_ROWS_SHOULD_NOT_LOAD")) as sql, \
             patch.object(index, "start_background_refresh",
                          side_effect=AssertionError("INDEX_WORKER_SHOULD_NOT_START")) as worker:
            with pytest.raises(re.error):
                index.search("[", regex=True)
        git.assert_not_called()
        refresh.assert_not_called()
        sql.assert_not_called()
        worker.assert_not_called()
    finally:
        index.close()


def test_valid_case_sensitive_regex_still_finds_exact_source_line(tmp_path):
    (tmp_path / "module.py").write_text(
        "def SELECTOR():\n    pass\ndef selector():\n    pass\n",
        encoding="utf-8",
    )
    index = Indexer(tmp_path)
    try:
        index.refresh()
        results = index.search(r"def\s+SELECTOR", regex=True, case_sensitive=True)
        assert [(row["file"], row["line"]) for row in results] == [
            ("module.py", 1)
        ]
        assert index.search(r"def\s+selector", regex=True, case_sensitive=True)[0]["line"] == 3
    finally:
        index.close()
