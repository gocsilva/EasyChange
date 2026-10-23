import pytest

from easychange.core.human_text_service import HumanTextService


@pytest.mark.parametrize(("payload", "expected_lines"), [
    (b"alpha\rbeta\r", 2),
    (b"alpha\rbeta\nlast", 3),
    (b"alpha\r\nbeta\r\n", 2),
    (b"alpha\nbeta\n", 2),
    (b"", 0),
])
def test_human_text_info_counts_cr_and_crlf_line_boundaries(tmp_path, payload, expected_lines):
    service = HumanTextService(tmp_path)
    service.path.write_bytes(payload)
    assert service.info()["lines"] == expected_lines


def test_human_text_crlf_across_streaming_block_boundary(tmp_path):
    service = HumanTextService(tmp_path)
    payload = b"x" * (1024 * 1024 - 1) + b"\r\ny"
    service.path.write_bytes(payload)
    assert service.info()["lines"] == 2
