from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_primary_launcher_does_not_pass_trailing_backslash_root_as_workspace():
    text = (ROOT / "Abrir EasyChange.bat").read_text(encoding="utf-8")

    assert 'pushd "%EASYCHANGE_ROOT%"' in text
    assert '"%EASYCHANGE_PYTHON%" -m easychange.gui "." --machine --hid' in text
    assert '-m easychange.gui "%EASYCHANGE_ROOT%" --machine --hid' not in text


def test_gui_launcher_still_uses_gui_module_without_shell_command_reconstruction():
    text = (ROOT / "Abrir_EasyChange_GUI.bat").read_text(encoding="utf-8")

    assert "-m easychange.gui" in text
    assert "cmd /c" not in text.casefold()
    assert "powershell" not in text.casefold()
