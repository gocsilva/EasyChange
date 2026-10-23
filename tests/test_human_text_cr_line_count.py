import pytest

from easychange.core.human_text_service import HumanTextService


@pytest.mark.parametrize(
    ("content", "expected_lines"),
    [
        ("alpha\\rbeta\\rgamma", 3),
        ("alpha\\r\\nbeta\\rgamma\\n", 3),
        ("alpha\\r", 1),
    ],
)
def test_saved_human_text_counts_cr_and_mixed_line_endings(tmp_path, content, expected_lines):
    content = content.replace("\\r", "\r").replace("\\n", "\n")
    service = HumanTextService(tmp_path)
    saved = service.save(content)
    assert saved["lines"] == expected_lines
    assert service.info()["lines"] == expected_lines
