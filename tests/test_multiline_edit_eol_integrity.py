"""Multiline source edits must preserve existing Windows line terminators."""
from easychange.core.file_service import FileService
from easychange.core.workspace import Workspace


def test_insert_line_preserves_crlf_and_does_not_normalize_other_lines(tmp_path):
    path=tmp_path/"x.cs"
    path.write_bytes(b"first\r\nsecond\r\nthird\r\n")
    FileService(Workspace(tmp_path,tmp_path.name)).insert_line("x.cs",2,"insert")
    assert path.read_bytes()==b"first\r\ninsert\r\nsecond\r\nthird\r\n"


def test_insert_at_end_without_final_newline_keeps_file_without_final_newline(tmp_path):
    path=tmp_path/"x.cs"
    path.write_bytes(b"first\r\nsecond")
    FileService(Workspace(tmp_path,tmp_path.name)).insert_line("x.cs",3,"last")
    assert path.read_bytes()==b"first\r\nsecond\r\nlast"


def test_replace_range_preserves_crlf_on_unmodified_lines(tmp_path):
    path=tmp_path/"x.cs"
    path.write_bytes(b"before\r\nold1\r\nold2\r\nafter\r\n")
    FileService(Workspace(tmp_path,tmp_path.name)).replace_range("x.cs",2,3,"new1\nnew2")
    assert path.read_bytes()==b"before\r\nnew1\r\nnew2\r\nafter\r\n"


def test_replace_range_preserves_classic_cr_and_unterminated_tail(tmp_path):
    path=tmp_path/"x.txt"
    path.write_bytes(b"first\rsecond\rthird")
    FileService(Workspace(tmp_path,tmp_path.name)).replace_range("x.txt",2,3,"replacement")
    assert path.read_bytes()==b"first\rreplacement"
