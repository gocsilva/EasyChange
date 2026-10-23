from easychange.core.indexer import Indexer


def test_indexer_updates_literal_filename_with_leading_space(tmp_path):
    (tmp_path / " secret.py").write_text("def real_secret_symbol():\n    return 1\n", encoding="utf-8")
    index = Indexer(tmp_path)
    result = index.update_path(" secret.py")
    assert result.get("updated") is True
    assert result["path"] == " secret.py"
    assert any(row["path"] == " secret.py" for row in index.files())


def test_protect_path_keeps_leading_space_and_scrubs_exact_file(tmp_path):
    (tmp_path / " secret.py").write_text("def private_secret_symbol():\n    return 1\n", encoding="utf-8")
    index = Indexer(tmp_path)
    index.refresh(force=True)
    assert any(row["path"] == " secret.py" for row in index.files())
    result = index.protect_path(" secret.py", scrub=False)
    assert result["path"] == " secret.py"
    assert " secret.py" in index.protected_paths()
    assert all(row["path"] != " secret.py" for row in index.files())
    assert index.symbols(name="private_secret_symbol") == []
