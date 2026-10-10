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


def test_git_prefix_is_literal_and_intersects_extension(tmp_path):
    (tmp_path / ".git").mkdir()
    index=Indexer(tmp_path)
    output=(
        "src_[x]/expected.py:1:TOKEN=valid\n"
        "src_[x]/wrong.cs:2:TOKEN=incorrect-extension\n"
        "src_any/escaped.py:3:TOKEN=wrong-path\n"
        "src_[x]other/sibling.py:4:TOKEN=wrong-prefix\n"
    )
    captured=[]
    def git_grep(argv, **kwargs):
        captured.append(argv)
        return subprocess.CompletedProcess(argv,0,output,"")

    with patch("easychange.core.indexer.subprocess.run",side_effect=git_grep):
        rows=index._git_search(
            "TOKEN",regex=False,extension="py",path_prefix="src_[x]",
            case_sensitive=False)

    assert rows==[("src_[x]/expected.py","1:TOKEN=valid")]
    assert captured[0][-1]==":(literal)src_[x]"
    assert not any(arg.startswith(":(glob)src_") for arg in captured[0])


@pytest.mark.parametrize("prefix", ["../outside", "/tmp/outside", "src/../outside"])
def test_git_prefix_rejects_workspace_traversal_before_subprocess(tmp_path,prefix):
    (tmp_path / ".git").mkdir()
    index=Indexer(tmp_path)
    with patch("easychange.core.indexer.subprocess.run") as runner:
        with pytest.raises(ValueError,match="INVALID_PROTECTED_INDEX_PATH"):
            index._git_search("TOKEN",regex=False,extension=None,
                              path_prefix=prefix,case_sensitive=False)
    runner.assert_not_called()


@pytest.mark.skipif(__import__("shutil").which("git") is None,
                    reason="Git executable is required for pathspec contract")
def test_real_git_grep_with_literal_directory_glob_characters(tmp_path):
    """Exercise Git's :(literal) pathspec instead of only mocking argv."""
    import subprocess
    project=tmp_path/"checkout"
    project.mkdir()
    (project/"src_[x]").mkdir()
    (project/"src_any").mkdir()
    (project/"src_[x]"/"target.py").write_text(
        "TOKEN=-n real\n",encoding="utf-8")
    (project/"src_[x]"/"other.cs").write_text(
        "TOKEN=-n excluded-extension\n",encoding="utf-8")
    (project/"src_any"/"unexpected.py").write_text(
        "TOKEN=-n wrong-directory\n",encoding="utf-8")
    subprocess.run(["git","init","-q"],cwd=project,check=True)
    subprocess.run(["git","add","--all"],cwd=project,check=True)
    index=Indexer(project)
    rows=index._git_search(
        "TOKEN=-n",regex=False,extension=".py",
        path_prefix="src_[x]",case_sensitive=True)
    assert rows==[("src_[x]/target.py","1:TOKEN=-n real")]


def test_cold_git_grep_resolves_workspace_root_once_for_many_matches(tmp_path):
    """Thousands of hits must not repeat slow workspace-root resolution."""
    from pathlib import Path

    (tmp_path/".git").mkdir()
    paths=[]
    for number in range(50):
        name=f"source_{number:03d}.py"
        (tmp_path/name).write_text("TOKEN = 1\n",encoding="utf-8")
        paths.append(name)
    index=Indexer(tmp_path)
    stdout="".join(f"{name}:1:TOKEN = 1\n" for name in paths)
    native=Path.resolve
    root_calls=[]

    def measured(path,*args,**kwargs):
        if path==tmp_path:
            root_calls.append(1)
        return native(path,*args,**kwargs)

    response=subprocess.CompletedProcess(
        ["git","grep"],0,stdout,"")
    with patch.object(Path,"resolve",measured), \
         patch("easychange.core.indexer.subprocess.run",return_value=response):
        matches=index._git_search(
            "TOKEN",regex=False,extension=None,path_prefix=None,
            case_sensitive=False)
    assert len(matches)==len(paths)
    assert len(root_calls)==1
    assert matches[0]==("source_000.py","1:TOKEN = 1")
