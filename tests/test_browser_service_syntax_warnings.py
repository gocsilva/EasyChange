from pathlib import Path
import warnings


def test_browser_service_has_no_invalid_string_escape_warnings():
    source_path = Path(__file__).parents[1] / "easychange" / "core" / "browser_service.py"
    source = source_path.read_text(encoding="utf-8")

    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        warnings.simplefilter("error", DeprecationWarning)
        compile(source, str(source_path), "exec")
