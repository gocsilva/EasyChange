from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_primary_launcher_restores_last_project():
    text = (ROOT / "Abrir EasyChange.bat").read_text(encoding="utf-8")

    assert 'pushd "%EASYCHANGE_ROOT%"' in text
    assert '"%EASYCHANGE_PYTHON%" -m easychange.gui "." --machine --hid' in text
    assert '-m easychange.gui "%EASYCHANGE_ROOT%" --machine --hid' not in text


def test_gui_launcher_forces_human_mode_using_project_venv():
    text = (ROOT / "Abrir_EasyChange_GUI.bat").read_text(encoding="utf-8")

    assert 'set "EASYCHANGE_PY=%EASYCHANGE_ROOT%.venv\\Scripts\\python.exe"' in text
    assert 'set "EASYCHANGE_PYW=%EASYCHANGE_ROOT%.venv\\Scripts\\pythonw.exe"' in text
    assert '"%EASYCHANGE_PY%" -c "import PySide6; import easychange.gui"' in text
    assert '"%EASYCHANGE_PYW%" -m easychange.gui "." --human' in text
    assert "where pythonw" not in text.casefold()
    assert "where python " not in text.casefold()
    assert "cmd /c" not in text.casefold()
    assert "powershell" not in text.casefold()


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
