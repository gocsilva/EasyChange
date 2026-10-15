from pathlib import Path
from easychange.ui import recent_project

def test_recent_project_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(recent_project, "STATE_FILE", tmp_path / "state.json")
    project = tmp_path / "my project"
    project.mkdir()
    recent_project.remember_project(project)
    assert recent_project.last_project() == str(project.resolve())
    project.rmdir()
    assert recent_project.last_project() is None

def test_recent_project_corrupt_or_oversized(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    monkeypatch.setattr(recent_project, "STATE_FILE", state)
    state.write_text("{bad", encoding="utf-8")
    assert recent_project.last_project() is None
    state.write_text("x" * 8193, encoding="utf-8")
    assert recent_project.last_project() is None
