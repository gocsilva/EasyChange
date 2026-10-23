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
