"""Cold Git discovery must preserve literal workspace directory and file prefixes."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from easychange.core.indexer import Indexer


def test_cold_git_scope_with_brackets_is_literal_and_intersects_extension(tmp_path):
    project=tmp_path/"repo"
    project.mkdir()
    (project/".git").mkdir()
    (project/"src[1]").mkdir()
    (project/"src[1]"/"actual.py").write_text("def CANARY(): pass\n",encoding="utf-8")
    (project/"src[1]"/"actual.txt").write_text("def CANARY(): pass\n",encoding="utf-8")
    (project/"src1").mkdir()
    (project/"src1"/"unrelated.py").write_text("def CANARY(): pass\n",encoding="utf-8")
    index=Indexer(project)
    index.start_background_refresh=lambda:None

    output=("src[1]/actual.py:1:def CANARY(): pass\n"
            "src[1]/actual.txt:1:def CANARY(): pass\n"
            "src1/unrelated.py:1:def CANARY(): pass\n")
    with patch("easychange.core.indexer.subprocess.run",
               return_value=subprocess.CompletedProcess(
                   ["git","grep"],0,output,"")) as run:
        result=index.discover_many(["CANARY"],path_prefix="src[1]",
                                    extension=".py")
    args=run.call_args.args[0]
    assert ":(literal)src[1]" in args
    assert not any(":(glob)src[1]" in arg for arg in args)
    assert result["files"]==["src[1]/actual.py"]
    assert [v["file"] for v in result["by_term"]["CANARY"]["definitions"]]==[
        "src[1]/actual.py"]


def test_cold_git_scope_accepts_one_concrete_file_not_directory_glob(tmp_path):
    (tmp_path/".git").mkdir()
    folder=tmp_path/"src"
    folder.mkdir()
    (folder/"target.py").write_text("def CANARY(): pass\n",encoding="utf-8")
    (folder/"other.py").write_text("def CANARY(): pass\n",encoding="utf-8")
    index=Indexer(tmp_path)
    index.start_background_refresh=lambda:None

    with patch("easychange.core.indexer.subprocess.run",
               return_value=subprocess.CompletedProcess(
                   ["git","grep"],0,
                   "src/target.py:1:def CANARY(): pass\n"
                   "src/other.py:1:def CANARY(): pass\n","")) as run:
        result=index.discover_many(
            ["CANARY"],path_prefix="src/target.py",extension=".py")
    argv=run.call_args.args[0]
    assert argv[-1]==":(literal)src/target.py"
    assert result["files"]==["src/target.py"]
