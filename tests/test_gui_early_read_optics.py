"""Native read EC2 QR must be queued before focus and widget work."""
from types import SimpleNamespace

from easychange.ui.main_window import MainWindow


def _run_stub(sequence, command, text=":dummy"):
    events = []

    class Result:
        ok = True
        data = {}
        def __init__(self):
            self.sequence = sequence
            self.command = command
            self.data = {}
        def render(self, _mode):
            events.append("render")
            return "ready"

    result = Result()
    window = SimpleNamespace()
    window.service = SimpleNamespace(
        execute=lambda text: result, machine=True, output="compact"
    )
    window._machine = True
    window._refresh_header = lambda: events.append("refresh")
    window._sync_optical_transport_controls = lambda: events.append("controls")
    window.focus_command = lambda: (events.append("focus") or
                                    {"command_focus_confirmed": True,
                                     "focus_restore_second_pass": True,
                                     "focus_method": "active-window-restore"})
    window.output = SimpleNamespace(
        setPlainText=lambda text: events.append("output")
    )
    window._update_machine_qr = lambda value: events.append("optics")
    MainWindow._run_command(window, text)
    return events, result


def test_sequenced_native_read_publishes_optics_before_focus():
    events, _result = _run_stub("QFASTREAD123", "read")
    assert events.count("optics") == 1
    assert events.index("optics") < events.index("focus")
    assert events.index("optics") < events.index("output")


def test_unsequenced_or_other_command_preserves_focus_first():
    for seq, command in ((None, "read"), ("QFASTSTATE123", "state")):
        events, _result = _run_stub(seq, command)
        assert events.count("optics") == 1
        assert events.index("focus") < events.index("optics")


def test_sequenced_compact_state_omits_verbose_focus_but_keeps_confirmation():
    events, result = _run_stub(
        "QFASTSTATE123",
        "state",
        ":ec QFASTSTATE123 :state --compact",
    )

    assert "easychange_focus" not in result.data
    assert result.data["command_focus_confirmed"] is True
    assert events.index("focus") < events.index("optics")


def test_unsequenced_compact_state_keeps_focus_diagnostics():
    _events, result = _run_stub(None, "state", ":state --compact")

    assert result.data["easychange_focus"]["focus_restore_second_pass"] is True
    assert result.data["command_focus_confirmed"] is True
