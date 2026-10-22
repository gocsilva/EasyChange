
"""GATT retry and diagnostic commands are keyboard-accessible over USB HID."""
from pathlib import Path

GUI = (Path(__file__).resolve().parents[1] / "easychange/ui/main_window.py").read_text(
    encoding="utf-8"
)


def test_gatt_keyboard_shortcuts_only_call_existing_local_operator_actions():
    assert 'self._add_shortcut("Ctrl+Shift+G", self._retry_gatt_connection)' in GUI
    assert 'self._add_shortcut("Ctrl+Shift+D", self.open_gatt_diagnostics)' in GUI
    assert 'self.gatt_retry_button.clicked.connect(self._retry_gatt_connection)' in GUI
    assert 'self.gatt_diagnostic_button.clicked.connect(self.open_gatt_diagnostics)' in GUI
    assert 'def _retry_gatt_connection(self)' in GUI
    assert 'def open_gatt_diagnostics(self)' in GUI
    retry_body=GUI.split('def _retry_gatt_connection(self)',1)[1].split(
        'def open_gatt_diagnostics',1)[0]
    assert 'self._gatt_client.retry_now()' in retry_body
    assert 'remove_device' not in retry_body.lower()
    assert 'pair(' not in retry_body.lower()


def test_gatt_keyboard_shortcuts_do_not_override_existing_easychange_hotkeys():
    assert 'self._add_shortcut("Ctrl+K", self.focus_command)' in GUI
    assert 'self._add_shortcut("Ctrl+D", lambda: self._run_command(":diff"))' in GUI
    assert 'self._add_shortcut("Ctrl+G", lambda: self._prompt(":goto "))' in GUI
    assert GUI.count('self._add_shortcut("Ctrl+Shift+G"') == 1
    assert GUI.count('self._add_shortcut("Ctrl+Shift+D"') == 1
