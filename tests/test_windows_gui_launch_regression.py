"""Regressions for Windows EasyChange GUI launch and Git output decoding."""

from types import SimpleNamespace

from easychange.core.git_service import GitService
from scripts import launch_easychange_gui as launcher


def test_git_run_never_uses_windows_default_charmap(monkeypatch, tmp_path):
    seen = {}

    def fake_git_run(*argv, **kwargs):
        seen.update(kwargs)
        # Windows Git can emit valid UTF-8 with bytes undefined in cp1252.
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        return SimpleNamespace(stdout=" M documentação/ação.cs\n", stderr="", returncode=0)

    monkeypatch.setattr("easychange.core.git_service.subprocess.run", fake_git_run)
    service = GitService(SimpleNamespace(root_path=tmp_path))
    assert "documentação" in service._run("status", "--short")
    assert seen["text"] is True


def test_gui_launcher_captures_child_errors_using_consoleless_python(tmp_path):
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"stub")
    (scripts / "pythonw.exe").write_bytes(b"stub")
    seen = {}

    def child(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return SimpleNamespace(pid=4242, poll=lambda: 5)

    result = launcher.ensure_gui(
        tmp_path,
        find_windows_fn=lambda: [],
        popen_factory=child,
    )
    assert result["error"] == "GUI_PROCESS_EXITED_BEFORE_WINDOW"
    assert result["return_code"] == 5
    assert seen["argv"][0].endswith("python.exe")
    assert seen["kwargs"]["env"]["PYTHONUTF8"] == "1"
    assert seen["kwargs"]["env"]["PYTHONIOENCODING"] == "utf-8:replace"
    assert seen["kwargs"]["stderr"] == launcher.subprocess.STDOUT
    assert (tmp_path / ".easychange" / "gui-launch.log").exists()
