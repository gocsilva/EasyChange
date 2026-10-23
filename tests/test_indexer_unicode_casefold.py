"""Warm SQLite index must match Unicode using Python's casefold semantics."""
from easychange.core.indexer import Indexer


def test_warm_index_finds_short_unicode_casefold_matches(tmp_path):
    (tmp_path / "sample.py").write_text('message = "Élan"\n', encoding="utf-8")
    index = Indexer(tmp_path)
    try:
        index.update_path("sample.py")
        index._fully_indexed = True
        insensitive = index.search("é", case_sensitive=False)
        exact = index.search("É", case_sensitive=True)
        assert [item["file"] for item in insensitive] == ["sample.py"]
        assert [item["file"] for item in exact] == ["sample.py"]
        assert index.search("é", case_sensitive=True) == []
    finally:
        index.close()
