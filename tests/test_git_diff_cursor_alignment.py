"""Protect Git diff byte paging from dead cursors inside UTF-8 codepoints."""
from easychange.core.git_service import GitService


def test_utf8_diff_cursor_inside_multibyte_character_makes_forward_progress():
    content=b"A" + "€".encode("utf-8") + b"Z" * 800
    # index 2/3 are continuation bytes, not valid UTF-8 starts.
    for cursor in (2,3):
        text, nxt = GitService._utf8_page(content,cursor,256)
        assert nxt > cursor, "Unaligned caller cursor must never loop forever"
        assert text
        assert text.startswith("Z")
        assert "�" not in text


def test_utf8_diff_paging_keeps_every_character_across_page_boundary():
    content = ("a" * 255 + "€" + "b" * 500).encode("utf-8")
    recovered=""
    cursor=0
    for _ in range(8):
        text,nxt=GitService._utf8_page(content,cursor,256)
        assert nxt>cursor
        recovered+=text
        cursor=nxt
        if cursor >= len(content):break
    assert recovered.encode("utf-8")==content
