"""Warm multi-term discovery must respect the same path/extension boundary as cold Git discovery."""
from __future__ import annotations

from easychange.core.indexer import Indexer


def _source(root, relative, content):
    target=root/relative
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(content,encoding="utf-8")


def test_warm_discovery_filters_definitions_before_applying_count_limit(tmp_path):
    # An out-of-scope symbol can sort before the one the caller requested.
    _source(tmp_path,"a_outside.py","def CANARY():\n    return 'outside'\n")
    _source(tmp_path,"src/target.py","def CANARY():\n    return 'inside'\n")
    _source(tmp_path,"src/other.txt","def CANARY():\n    return 'wrong extension'\n")
    index=Indexer(tmp_path)
    index.refresh(force=True)

    report=index.discover_many(
        ["CANARY"],path_prefix="src",extension=".py",
        definitions_per_term=1,references_per_term=20)
    definitions=report["by_term"]["CANARY"]["definitions"]
    assert [(entry["file"],entry["name"]) for entry in definitions]==[
        ("src/target.py","CANARY")]
    assert all(item["file"]=="src/target.py"
               for item in report["by_term"]["CANARY"]["references"])
    assert report["files"]==["src/target.py"]


def test_warm_discovery_can_restrict_definitions_to_literal_file_prefix(tmp_path):
    _source(tmp_path,"src/target.py","def CANARY():\n    return 'selected'\n")
    _source(tmp_path,"src/target.py.bak","def CANARY():\n    return 'not selected'\n")
    _source(tmp_path,"src/nested/other.py","def CANARY():\n    return 'not selected'\n")
    index=Indexer(tmp_path)
    index.refresh(force=True)

    filtered=index.discover_many(["CANARY"],path_prefix="src/target.py",
                                 extension="py")
    assert [v["file"] for v in filtered["by_term"]["CANARY"]["definitions"]]==[
        "src/target.py"]
    assert set(filtered["files"])=={"src/target.py"}

    # Removing the scope must retain ordinary global discovery.
    unrestricted=index.discover_many(["CANARY"],extension=".py")
    assert {v["file"] for v in unrestricted["by_term"]["CANARY"]["definitions"]}=={
        "src/target.py","src/nested/other.py"}
