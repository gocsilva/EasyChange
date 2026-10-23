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
