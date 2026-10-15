from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_primary_launcher_uses_recent_project_without_explicit_workspace():
    content = (ROOT / "Abrir EasyChange.bat").read_text(encoding="utf-8")
    assert 'pushd "%EASYCHANGE_ROOT%"' in content
    assert '"%EASYCHANGE_PYTHON%" -m easychange.gui --machine --hid' in content
    assert '-m easychange.gui "." --machine --hid' not in content


def test_gui_launcher_uses_deduplicating_python_entrypoint():
    content = (ROOT / "Abrir_EasyChange_GUI.bat").read_text(encoding="utf-8")
    assert 'set "EASYCHANGE_PY=%EASYCHANGE_ROOT%.venv\\Scripts\\python.exe"' in content
    assert '"%EASYCHANGE_PY%" "scripts\\launch_easychange_gui.py" "."' in content
    assert "powershell" not in content.casefold()


def test_gui_cli_human_flag_is_explicit_and_mutually_exclusive():
    from easychange.gui import build_parser
    parser = build_parser()
    args = parser.parse_args([".", "--human"])
    assert args.workspace == "."
    assert args.human is True
    assert args.machine is False
    assert args.hid is False


def test_gui_cli_without_workspace_can_restore_recent_project():
    from easychange.gui import build_parser
    args = build_parser().parse_args(["--human"])
    assert args.workspace is None
