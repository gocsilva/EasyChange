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


def test_cold_git_output_has_bounded_disk_spool_and_explicit_truncation(
        tmp_path, monkeypatch):
    import easychange.core.indexer as source

    (tmp_path / ".git").mkdir()
    (tmp_path / "source.py").write_text(
        "def CANARY(): pass\n", encoding="utf-8")
    index = Indexer(tmp_path)
    index.start_background_refresh = lambda: None
    monkeypatch.setattr(source, "MAX_COLD_GIT_BYTES", 80)

    def fake_git(argv, **kwargs):
        # The real subprocess streams into a temporary file, not a captured
        # CompletedProcess.stdout allocation. Model output beyond the cap.
        assert kwargs.get("capture_output") is None
        assert kwargs["stdout"].writable()
        kwargs["stdout"].write(
            b"source.py:1:def CANARY(): pass\n" + b"x" * 500)
        return subprocess.CompletedProcess(argv, 0, None, None)

    with patch("easychange.core.indexer.subprocess.run",
               side_effect=fake_git) as called:
        report = index.discover_many(["CANARY"])
    called.assert_called_once()
    assert report["truncated"] is True
    assert report["files"] == ["source.py"]
    assert len(report["by_term"]["CANARY"]["definitions"]) == 1


def test_cold_git_bounds_number_of_retained_match_rows(tmp_path, monkeypatch):
    import easychange.core.indexer as source

    (tmp_path / ".git").mkdir()
    (tmp_path / "source.py").write_text(
        "def CANARY(): pass\n" * 5, encoding="utf-8")
    index = Indexer(tmp_path)
    index.start_background_refresh = lambda: None
    monkeypatch.setattr(source, "MAX_COLD_GIT_ROWS", 2)
    matches = "".join(
        f"source.py:{line}:def CANARY(): pass\n" for line in range(1, 6))
    with patch("easychange.core.indexer.subprocess.run",
               return_value=subprocess.CompletedProcess(
                   ["git", "grep"], 0, matches, "")):
        report = index.discover_many(["CANARY"])
    assert report["truncated"] is True
    assert len(report["by_term"]["CANARY"]["definitions"]) == 2
    assert report["by_term"]["CANARY"]["occurrences"] == 2


def test_single_term_cold_grep_avoids_unbounded_capture(tmp_path, monkeypatch):
    import easychange.core.indexer as source

    (tmp_path / ".git").mkdir()
    (tmp_path / "source.py").write_text(
        "def CANARY(): pass\n", encoding="utf-8")
    index = Indexer(tmp_path)
    monkeypatch.setattr(source, "MAX_COLD_GIT_BYTES", 64)

    def oversized_result(argv, **kwargs):
        assert kwargs.get("capture_output") is None
        kwargs["stdout"].write(
            b"source.py:1:def CANARY(): pass\n" + b"x" * 150)
        return subprocess.CompletedProcess(argv, 0, None, None)

    with patch("easychange.core.indexer.subprocess.run",
               side_effect=oversized_result):
        assert index._git_search(
            "CANARY", regex=False, extension=".py", path_prefix=None,
            case_sensitive=True) == []


def test_single_term_cold_grep_caps_row_count(tmp_path, monkeypatch):
    import easychange.core.indexer as source

    (tmp_path / ".git").mkdir()
    (tmp_path / "source.py").write_text(
        "def CANARY(): pass\n" * 4, encoding="utf-8")
    index = Indexer(tmp_path)
    monkeypatch.setattr(source, "MAX_COLD_GIT_ROWS", 2)
    with patch("easychange.core.indexer.subprocess.run",
               return_value=subprocess.CompletedProcess(
                   ["git", "grep"], 0,
                   "".join(f"source.py:{i}:def CANARY(): pass\n"
                           for i in range(1, 5)), "")):
        found = index._git_search(
            "CANARY", regex=False, extension=".py", path_prefix=None,
            case_sensitive=True)
    assert len(found) == 2
    assert found[0][0] == "source.py"



def test_cold_multi_term_rejects_directory_symlink_to_protected_workspace(tmp_path):
    project=tmp_path/"project"
    project.mkdir()
    (project/".git").mkdir()
    protected=project/".easychange"
    protected.mkdir()
    (protected/"private.py").write_text(
        "def CANARY():\n    return 'never reveal'\n",encoding="utf-8")
    alias=project/"source_alias"
    try:
        alias.symlink_to(protected,target_is_directory=True)
    except (NotImplementedError,OSError):
        pytest.skip("directory symlinks unavailable")
    normal=project/"safe.py"
    normal.write_text("def CANARY():\n    pass\n",encoding="utf-8")

    index=Indexer(project)
    index.start_background_refresh=lambda:None
    lines=("source_alias/private.py:1:def CANARY():\n"
           "safe.py:1:def CANARY():\n")
    with patch("easychange.core.indexer.subprocess.run",
               return_value=subprocess.CompletedProcess(
                   ["git","grep"],0,lines,"")) as runner:
        result=index.discover_many(["CANARY"])
    runner.assert_called_once()
    assert result["files"]==["safe.py"]
    assert all(item["file"]=="safe.py"
               for item in result["by_term"]["CANARY"]["definitions"])
    assert all(item["file"]=="safe.py"
               for item in result["by_term"]["CANARY"]["references"])


@pytest.mark.parametrize("invalid", (
    "", "\x00", "CANARY\x00after", "x" * 513, None, 123,
))
def test_single_search_rejects_unbounded_invalid_query_before_git(
        tmp_path, invalid):
    (tmp_path / ".git").mkdir()
    index = Indexer(tmp_path)
    with patch("easychange.core.indexer.subprocess.run") as git:
        with pytest.raises(ValueError,match="INVALID_INDEX_QUERY"):
            index.search(invalid)
    git.assert_not_called()


@pytest.mark.parametrize("invalid", ("x" * 513, "SAFE\x00SECRET"))
def test_multi_term_discovery_fails_before_git_on_bad_term(tmp_path, invalid):
    (tmp_path / ".git").mkdir()
    index = Indexer(tmp_path)
    with patch("easychange.core.indexer.subprocess.run") as git:
        with pytest.raises(ValueError,match="INVALID_INDEX_TERM"):
            index.discover_many(["CANARY",invalid])
    git.assert_not_called()
