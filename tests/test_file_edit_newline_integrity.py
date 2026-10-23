"""Local file edit regressions: newline fidelity matters for C# and .NET repos."""
from easychange.core.file_service import FileService
from easychange.core.workspace import Workspace


def test_replace_line_preserves_original_crlf_and_neighbor_lines(tmp_path):
    target=tmp_path/"sample.cs"
    target.write_bytes(b"class A\r\n{\r\n  return 1;\r\n}\r\n")
    service=FileService(Workspace(tmp_path,tmp_path.name))
    result=service.replace_line("sample.cs",3,"  return 2;")
    assert result["line"]==3
    assert target.read_bytes()==b"class A\r\n{\r\n  return 2;\r\n}\r\n"


def test_replace_line_preserves_classic_cr_line_endings(tmp_path):
    target=tmp_path/"sample.txt"
    target.write_bytes(b"first\rsecond\rthird\r")
    service=FileService(Workspace(tmp_path,tmp_path.name))
    service.replace_line("sample.txt",2,"new")
    assert target.read_bytes()==b"first\rnew\rthird\r"



def test_read_never_caches_old_bytes_under_new_file_stat(tmp_path, monkeypatch):
    """A concurrent local editor cannot make the hot cache serve stale C# code."""
    target=tmp_path/"payment.cs"
    target.write_bytes(b"OLD_REMOTE_CODE\n")
    service=FileService(Workspace(tmp_path,tmp_path.name))
    original=service._read_limited_text
    mutated=[False]

    def mutate_once(path):
        snapshot=original(path)
        if not mutated[0]:
            mutated[0]=True
            path.write_bytes(b"NEW_REMOTE_CODE_WITH_PATCH\n")
        return snapshot

    monkeypatch.setattr(service,"_read_limited_text",mutate_once)
    first=service.read("payment.cs")
    second=service.read("payment.cs")
    assert "NEW_REMOTE_CODE_WITH_PATCH" in str(first["lines"])
    assert "NEW_REMOTE_CODE_WITH_PATCH" in str(second["lines"])
    assert second["hash"]==first["hash"]


import pytest
from easychange.core.file_service import ExternalChangeError


def test_append_must_not_overwrite_concurrent_external_change(tmp_path,monkeypatch):
    target=tmp_path/"payment.cs"
    target.write_bytes(b"original\n")
    service=FileService(Workspace(tmp_path,tmp_path.name))
    original=service._read_limited_text
    modified=[False]
    def concurrent_edit(path):
        snapshot=original(path)
        if not modified[0]:
            modified[0]=True
            path.write_bytes(b"external-change\n")
        return snapshot
    monkeypatch.setattr(service,"_read_limited_text",concurrent_edit)
    with pytest.raises(ExternalChangeError):
        service.append("payment.cs","addition")
    assert target.read_bytes()==b"external-change\n"
