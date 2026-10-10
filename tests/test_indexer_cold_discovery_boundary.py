"""Cold multi-term discovery never returns protected or out-of-workspace source."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from easychange.core.indexer import Indexer


def test_cold_discovery_enforces_protection_extension_scope_and_symlinks(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    folder = project / "src"
    folder.mkdir()
    (folder / "allowed.py").write_text("def CANARY(): pass\n", encoding="utf-8")
    (folder / "protected.py").write_text("def CANARY(): pass\n", encoding="utf-8")
    (folder / "not_python.txt").write_text("def CANARY(): pass\n", encoding="utf-8")
    (project / "out_of_scope.py").write_text("def CANARY(): pass\n", encoding="utf-8")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "ignored.py").write_text(
        "def CANARY(): pass\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("def CANARY(): pass\n", encoding="utf-8")
    link = folder / "link.py"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        pass

    index = Indexer(project)
    index.protect_path("src/protected.py", scrub=False)
    # No background disk scan is necessary to exercise the Git cold path.
    index.start_background_refresh = lambda: None
    lines = [
        "src/allowed.py:1:def CANARY(): pass",
        "src/protected.py:1:def CANARY(): pass",
        "src/not_python.txt:1:def CANARY(): pass",
        "out_of_scope.py:1:def CANARY(): pass",
        "node_modules/ignored.py:1:def CANARY(): pass",
        "../outside.py:1:def CANARY(): pass",
        "/tmp/outside.py:1:def CANARY(): pass",
        "src/../../outside.py:1:def CANARY(): pass",
        "src/link.py:1:def CANARY(): pass",
        "src/absent.py:1:def CANARY(): pass",
    ]
    with patch(
        "easychange.core.indexer.subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["git", "grep"], 0, "\n".join(lines) + "\n", ""),
    ) as run:
        report = index.discover_many(
            ["CANARY"], extension=".py", path_prefix="src")
    run.assert_called_once()
    assert report["engine"] == "git-grep"
    assert report["files"] == ["src/allowed.py"]
    bucket = report["by_term"]["CANARY"]
    assert bucket["files"] == ["src/allowed.py"]
    assert all(item["file"] == "src/allowed.py"
               for item in bucket["references"])
    assert all(item["file"] == "src/allowed.py"
               for item in bucket["definitions"])


@pytest.mark.parametrize("scope", (
    "../private", "src/../../private", "/tmp/private",
    "C:/outside", "src//nested", ".",
))
def test_cold_discovery_rejects_nonliteral_outside_scope_before_git(
        tmp_path, scope):
    (tmp_path / ".git").mkdir()
    index = Indexer(tmp_path)
    with patch("easychange.core.indexer.subprocess.run") as git:
        with pytest.raises(ValueError, match="INVALID_PROTECTED_INDEX_PATH"):
            index.discover_many(["CANARY"], path_prefix=scope)
    git.assert_not_called()
