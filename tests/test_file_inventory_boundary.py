"""File inventory must not expose ignored runtime paths or foreign aliases."""
import os
from easychange.core.file_service import FileService
from easychange.core.workspace import Workspace


def test_file_inventory_prunes_case_variant_private_directories(tmp_path):
    root=tmp_path/"project"
    root.mkdir()
    for folder in (".GIT",".EASYCHANGE",".VENV"):
        (root/folder).mkdir()
        (root/folder/"secret.txt").write_text("secret")
    (root/"public.cs").write_text("public")
    result=FileService(Workspace(root,root.name)).list_files(limit=20)
    assert [f["path"] for f in result]==["public.cs"]


def test_file_inventory_skips_external_hardlinks_and_symlink_files(tmp_path):
    root=tmp_path/"project"
    root.mkdir()
    private=tmp_path/"private.dat"
    private.write_bytes(b"confidential")
    os.link(private,root/"foreign.txt")
    (root/"public.cs").write_text("public")
    results=FileService(Workspace(root,root.name)).list_files(limit=20)
    assert [f["path"] for f in results]==["public.cs"]
