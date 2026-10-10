from __future__ import annotations

import json
import subprocess
from pathlib import Path

import scripts.ensure_venv as ensure_venv


def _write_project(root: Path, extra: str = "") -> None:
    (root / "pyproject.toml").write_text(
        """
[project]
name = "sample"
version = "0.0.1"
requires-python = ">=3.10"
dependencies = []
""" + extra,
        encoding="utf-8",
    )


def test_dependency_fingerprint_changes_when_manifest_changes(tmp_path):
    _write_project(tmp_path)
    first = ensure_venv.dependency_fingerprint(tmp_path)
    _write_project(tmp_path, "\n# dependency-change\n")
    second = ensure_venv.dependency_fingerprint(tmp_path)
    assert first != second


def test_large_lock_fingerprint_streams_without_loading_whole_file(tmp_path, monkeypatch):
    import hashlib

    _write_project(tmp_path)
    lock=tmp_path/"uv.lock"
    payload=b"package = 'abc'\n" * 30000
    lock.write_bytes(payload)

    expected=hashlib.sha256()
    expected.update(b"easychange-venv-state-v1\0")
    expected.update(ensure_venv.DEFAULT_INSTALL_SPEC.encode("utf-8"))
    expected.update(b"\0")
    for manifest in ensure_venv.dependency_manifests(tmp_path):
        expected.update(manifest.relative_to(tmp_path).as_posix().encode("utf-8"))
        expected.update(b"\0")
        expected.update(payload if manifest==lock else
                        manifest.read_text(encoding="utf-8").encode("utf-8"))
        expected.update(b"\0")

    def unbounded_read(_path):
        raise AssertionError("UNBOUNDED_MANIFEST_READ")

    monkeypatch.setattr(Path,"read_bytes",unbounded_read)
    assert ensure_venv.dependency_fingerprint(tmp_path)==expected.hexdigest()


def test_missing_venv_is_detected_without_side_effects_in_dry_run(tmp_path):
    _write_project(tmp_path)
    result = ensure_venv.ensure(tmp_path, dry_run=True)
    assert result["needs_sync"] is True
    assert result["action"] == "would_sync"
    assert "venv_missing_or_python_broken" in result["reasons"]
    assert not (tmp_path / ".venv").exists()


def test_healthy_matching_venv_is_a_noop(tmp_path, monkeypatch):
    _write_project(tmp_path)
    venv = tmp_path / ".venv"
    python = ensure_venv.venv_python(venv)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")

    fingerprint = ensure_venv.dependency_fingerprint(tmp_path)
    ensure_venv._atomic_write_json(
        ensure_venv.stamp_path(venv),
        {
            "schema": 1,
            "dependency_fingerprint": fingerprint,
            "install_spec": ensure_venv.DEFAULT_INSTALL_SPEC,
            "python_major_minor": "3.12",
            "python_executable": str(python),
            "updated_at": 1,
        },
    )
    monkeypatch.setattr(
        ensure_venv,
        "interpreter_info",
        lambda *args, **kwargs: {
            "version": [3, 12, 7],
            "executable": str(python),
        },
    )
    monkeypatch.setattr(
        ensure_venv,
        "dependencies_healthy",
        lambda *args, **kwargs: True,
    )

    result = ensure_venv.ensure(tmp_path)
    assert result["needs_sync"] is False
    assert result["changed"] is False
    assert result["action"] == "unchanged"


def test_manifest_change_triggers_single_sync_and_updates_stamp(tmp_path, monkeypatch):
    _write_project(tmp_path)
    venv = tmp_path / ".venv"
    python = ensure_venv.venv_python(venv)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")

    old_fingerprint = "0" * 64
    ensure_venv._atomic_write_json(
        ensure_venv.stamp_path(venv),
        {
            "schema": 1,
            "dependency_fingerprint": old_fingerprint,
            "install_spec": ensure_venv.DEFAULT_INSTALL_SPEC,
            "python_major_minor": "3.12",
            "python_executable": str(python),
            "updated_at": 1,
        },
    )

    calls = []

    def fake_runner(argv, **kwargs):
        argv = [str(item) for item in argv]
        calls.append(argv)
        if "-c" in argv:
            code = argv[argv.index("-c") + 1]
            if "EASYCHANGE_VENV_OK" in code:
                return subprocess.CompletedProcess(argv, 0, "EASYCHANGE_VENV_OK\n", "")
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({
                    "version": [3, 12, 7],
                    "executable": str(python),
                }) + "\n",
                "",
            )
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(ensure_venv.sys, "executable", str(python))

    result = ensure_venv.ensure(tmp_path, runner=fake_runner)

    assert result["changed"] is True
    assert result["action"] == "synced"
    assert any(
        argv[-4:] == ["pip", "install", "-e", ".[gui]"]
        for argv in calls
    )
    stamp = ensure_venv.read_stamp(venv)
    assert stamp is not None
    assert stamp["dependency_fingerprint"] == ensure_venv.dependency_fingerprint(tmp_path)
    assert stamp["dependency_fingerprint"] != old_fingerprint

    calls.clear()
    again = ensure_venv.ensure(tmp_path, runner=fake_runner)
    assert again["action"] == "unchanged"
    assert not any("install" in argv for argv in calls)


def test_updater_invokes_venv_helper_instead_of_git_diff_heuristic():
    root = Path(__file__).resolve().parents[1]
    updater = (root / "Atualizar_EasyChange.bat").read_text(
        encoding="utf-8", errors="replace"
    )
    assert r"scripts\ensure_venv.py" in updater
    assert "git diff --name-only" not in updater
    assert "if not exist" not in updater or ".venv" not in updater.split("if not exist", 1)[-1]
    assert ":python_error" in updater
    assert ":dependency_error" in updater
