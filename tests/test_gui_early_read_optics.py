"""Native read EC2 QR must be queued before focus and widget work."""
from types import SimpleNamespace

from easychange.ui.main_window import MainWindow


def _run_stub(sequence, command):
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
                                    {"command_focus_confirmed": True})
    window.output = SimpleNamespace(
        setPlainText=lambda text: events.append("output")
    )
    window._update_machine_qr = lambda value: events.append("optics")
    MainWindow._run_command(window, ":dummy")
    return events


def test_sequenced_native_read_publishes_optics_before_focus():
    events = _run_stub("QFASTREAD123", "read")
    assert events.count("optics") == 1
    assert events.index("optics") < events.index("focus")
    assert events.index("optics") < events.index("output")


def test_unsequenced_or_other_command_preserves_focus_first():
    for seq, command in ((None, "read"), ("QFASTSTATE123", "state")):
        events = _run_stub(seq, command)
        assert events.count("optics") == 1
        assert events.index("focus") < events.index("optics")
