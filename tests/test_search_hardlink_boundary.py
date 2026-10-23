"""Search must never read external content through workspace hardlinks."""
import os

from easychange.core.search_service import SearchService
from easychange.core.workspace import Workspace


def test_search_excludes_external_hardlink_to_private_file(tmp_path):
    project=tmp_path/"project"
    project.mkdir()
    private=tmp_path/"private_document.txt"
    private.write_text("LEAK_TOKEN_PROTECTED_4077",encoding="utf-8")
    os.link(private,project/"borrowed.txt")
    (project/"normal.py").write_text("LEAK_TOKEN_PUBLIC_4077",encoding="utf-8")
    found=SearchService(Workspace.open(project)).search("LEAK_TOKEN",limit=10)
    assert [entry["file"] for entry in found]==["normal.py"]
    assert "LEAK_TOKEN_PROTECTED_4077" not in str(found)



def test_zero_or_negative_search_limits_produce_no_file_reads(tmp_path, monkeypatch):
    """A zero-result request cannot leak first matching line or scan files."""
    project=tmp_path/"project"
    project.mkdir()
    source=project/"normal.py"
    source.write_text("SEARCH_LIMIT_SECRET",encoding="utf-8")
    service=SearchService(Workspace.open(project))
    assert service.search("SEARCH_LIMIT_SECRET",limit=0)==[]
    assert service.search("SEARCH_LIMIT_SECRET",limit=-8)==[]



def test_search_skips_case_variant_git_and_runtime_directories(tmp_path):
    """Case-variant ignored folders must not leak Git metadata or runtime keys."""
    root=tmp_path/"project"
    root.mkdir()
    for folder in (".GIT", ".VENV", ".EASYCHANGE"):
        path=root/folder
        path.mkdir()
        (path/"sensitive.txt").write_text("PRIVATE_RUNTIME_MARKER_2026",encoding="utf-8")
    (root/"normal.py").write_text("PUBLIC_RUNTIME_MARKER_2026",encoding="utf-8")
    workspace=Workspace(root,root.name)
    assert SearchService(workspace).search("PRIVATE_RUNTIME_MARKER_2026")==[]
    assert len(SearchService(workspace).search("PUBLIC_RUNTIME_MARKER_2026"))==1
