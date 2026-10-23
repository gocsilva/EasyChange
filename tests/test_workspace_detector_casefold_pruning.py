import pytest

from easychange.core.workspace_detector import detect_workspace


@pytest.mark.parametrize(("directory", "marker"), [
    (".VENV", "pyproject.toml"),
    ("NODE_MODULES", "package.json"),
    ("BIN", "project.sln"),
    ("OBJ", "project.csproj"),
    ("TESTRESULTS", "pyproject.toml"),
])
def test_workspace_detection_prunes_case_variant_generated_directories(tmp_path, directory, marker):
    generated = tmp_path / directory
    generated.mkdir()
    (generated / marker).write_text("generated", encoding="utf-8")
    kind, adapters, projects = detect_workspace(tmp_path)
    assert (kind, adapters, projects) == ("GENERIC", ["GENERIC"], [])


def test_workspace_detection_keeps_real_nested_project_markers(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]", encoding="utf-8")
    kind, adapters, projects = detect_workspace(tmp_path)
    assert kind == "PYTHON"
    assert "PYTHON" in adapters
    assert projects == ["src/pyproject.toml"]
