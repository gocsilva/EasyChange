"""Cold Git index searches must treat user patterns as data, never CLI flags."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from easychange.core.indexer import Indexer


@pytest.mark.parametrize("query,regex", [
    ("-n", False),
    ("--help", False),
    ("-e", True),
    ("-foo.*", True),
])
def test_git_grep_leading_hyphen_cannot_be_interpreted_as_option(
        tmp_path, query, regex):
    (tmp_path / ".git").mkdir()
    (tmp_path / "source.py").write_text(
        "message = '--help and -n and -foo123'\n", encoding="utf-8")
    index=Indexer(tmp_path)
    captured=[]

    def git_grep(argv, **kwargs):
        captured.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 0, "source.py:1:message = '--help and -n and -foo123'\n", "")

    with patch("easychange.core.indexer.subprocess.run", side_effect=git_grep):
        rows=index._git_search(
            query, regex=regex, extension=None,
            path_prefix=None, case_sensitive=True)

    assert rows==[("source.py", "1:message = '--help and -n and -foo123'")]
    assert len(captured)==1
    argv=captured[0]
    assert argv[argv.index("-e")+1]==query
    assert argv.index("-e") < argv.index("--")
    assert argv[-1]=="--"
    assert ("-E" if regex else "-F") in argv
