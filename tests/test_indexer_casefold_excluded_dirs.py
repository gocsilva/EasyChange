"""Windows-style case-insensitive exclusions must protect source index boundaries."""
from easychange.core.indexer import Indexer


def test_case_variant_internal_directories_are_never_indexed(tmp_path):
    private = tmp_path / ".GIT"
    private.mkdir()
    (private / "config").write_text("SENSITIVE_GIT_CONFIG_MARKER\n", encoding="utf-8")
    env = tmp_path / ".VENV"
    env.mkdir()
    (env / "secrets.py").write_text("SENSITIVE_VENV_MARKER = 1\n", encoding="utf-8")
    (tmp_path / "public.py").write_text("PUBLIC_MARKER = 1\n", encoding="utf-8")

    index = Indexer(tmp_path)
    try:
        index.refresh(force=True)
        visible = [row["path"] for row in index.files()]
        assert visible == ["public.py"]
        assert index.update_path(".GIT/config")["ignored"] is True
        assert index.update_path(".VENV/secrets.py")["ignored"] is True
        assert index.search("SENSITIVE_GIT_CONFIG_MARKER") == []
        assert index.search("SENSITIVE_VENV_MARKER") == []
        assert index.search("PUBLIC_MARKER")
    finally:
        index.close()
