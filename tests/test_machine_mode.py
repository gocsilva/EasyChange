import json
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("qrcode")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from easychange.core.workspace import Workspace
from easychange.ui.main_window import MainWindow
import easychange.ui.main_window as main_window_module


def _drain_optical(window, app, limit: int = 512) -> None:
    for _ in range(limit):
        app.processEvents()
        if (
            window._optical_encode_future is None
            and window._optical_render_pending is None
        ):
            return
        QTest.qWait(1)
    raise AssertionError("optical renderer did not quiesce")


def test_machine_mode_keeps_command_focus_and_renders_sequence_qr(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()
    window._run_command(":ec QGUI1234 :state")
    _drain_optical(window, app)
    assert window.output.toPlainText().startswith("EC1 QGUI1234 OK")
    assert window.machine_qr.isVisible()
    assert window.machine_qr.pixmap() is not None

    window.editor.setFocus(); app.processEvents()
    app.processEvents()
    assert window.command.hasFocus()
    window.close(); app.processEvents()



def test_machine_mode_uses_compact_overflow_handoff_for_large_results(tmp_path):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "large.py"
    source.write_text(
        "\n".join(f"value_{i} = '{i:04d}-{i * 7919:08d}'" for i in range(900)),
        encoding="utf-8",
    )
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()
    window._run_command(":ec QGUI5678 :read large.py 1 900")
    _drain_optical(window, app)
    assert not window.tree.isVisible()
    assert len(window.machine_qrs) == 32
    assert len(window._optical_payloads) == 3
    assert window._optical_header_payload is not None
    assert window._optical_burst is False
    assert sum(
        1 for label in window.machine_qrs
        if label.pixmap() is not None and not label.pixmap().isNull()
    ) == 4
    assert all(
        label.width() == window._OPTICAL_COMPACT_QR_SIZE
        and label.height() == window._OPTICAL_COMPACT_QR_SIZE
        for label in window.machine_qrs
    )
    window.close(); app.processEvents()



def test_qr_pixmap_cache_is_bounded_and_keeps_complete_burst_page(tmp_path):
    from PySide6.QtGui import QPixmap

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    try:
        pixmap = QPixmap(16, 16)
        for index in range(100):
            window._cache_optical_pixmap(f"qr-{index}", pixmap)
        assert len(window._optical_pixmaps) == window._OPTICAL_PIXMAP_CACHE_ENTRIES
        assert "qr-0" not in window._optical_pixmaps
        assert all(f"qr-{index}" in window._optical_pixmaps
                   for index in range(84, 100))
        # A tight byte budget must not evict the 32 symbols that can be
        # simultaneously displayed on a single HDMI burst page.
        window._OPTICAL_PIXMAP_CACHE_BYTES = 200
        for index in range(100, 145):
            window._cache_optical_pixmap(f"qr-{index}", pixmap)
        assert len(window._optical_pixmaps) == window._OPTICAL_BURST_SLOTS
        assert all(f"qr-{index}" in window._optical_pixmaps
                   for index in range(113, 145))
    finally:
        window.close()
        app.processEvents()


def test_machine_mode_reuses_cached_qr_pixmaps(tmp_path):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "large.py"
    source.write_text(
        "\n".join(f"value_{i} = '{i:04d}-{i * 15485863:08d}'" for i in range(500)),
        encoding="utf-8",
    )
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()
    window._run_command(":ec QCACHE123 :read large.py 1 500")
    _drain_optical(window, app)
    first_payload = window._optical_payloads[0]
    first_pixmap = window._optical_pixmaps[first_payload]
    window._optical_page = 0
    window._render_optical_page()
    _drain_optical(window, app)
    assert window._optical_pixmaps[first_payload].cacheKey() == first_pixmap.cacheKey()
    window.close(); app.processEvents()



def test_optical_burst_pads_final_page_to_all_32_visible_slots(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()
    window._optical_payloads = [f"payload-{index}" for index in range(40)]
    window._optical_burst = True
    window._optical_page = 1
    window._render_optical_page()
    _drain_optical(window, app)
    assert all(label.pixmap() is not None for label in window.machine_qrs)
    window.close(); app.processEvents()



def test_five_chunk_result_enters_single_page_burst_with_redundancy(tmp_path, monkeypatch):
    from easychange.core.result import Result

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    monkeypatch.setattr(
        "easychange.ui.main_window.encode_result_bundle",
        lambda result, **kwargs: (
            [json.dumps({"p":"EC2","s":"QX","i":i,"n":5,"d":str(i)}, separators=(",", ":")) for i in range(5)],
            '{"p":"EC2H","s":"QX","n":5}',
        ),
    )
    window._update_machine_qr(Result(True, "study", {"x": 1}, sequence="QX"))
    _drain_optical(window, app)
    assert window._optical_burst is True
    assert window._optical_timer.isActive() is False
    assert len(window._optical_payloads) == 5
    assert sum(1 for label in window.machine_qrs if label.pixmap() and not label.pixmap().isNull()) == 32
    window.close(); app.processEvents()

def test_optical_recovery_renders_original_packets_without_nested_envelope(tmp_path):
    from easychange.core.result import Result

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    packet0 = {
        "p": "EC2", "v": 2, "s": "QORIGINAL1", "i": 0, "n": 2,
        "h": "abcd1234", "c": "11223344", "x": "55667788", "d": "AAA",
    }
    packet1 = {
        "p": "EC2", "v": 2, "s": "QORIGINAL1", "i": 1, "n": 2,
        "h": "abcd1234", "c": "11223344", "x": "99aabbcc", "d": "BBB",
    }
    recovery = Result(
        True,
        "optical_chunks",
        {
            "target_sequence": "QORIGINAL1",
            "requested": [0, 1],
            "packets": [packet0, packet1],
        },
        sequence="QRECOVERY1",
    )

    window._update_machine_qr(recovery)
    _drain_optical(window, app)

    assert len(window._optical_payloads) == 2
    decoded = [json.loads(payload) for payload in window._optical_payloads]
    assert decoded == [packet0, packet1]
    assert all(item["s"] == "QORIGINAL1" for item in decoded)
    assert all(item["s"] != "QRECOVERY1" for item in decoded)
    assert window._optical_header_payload is None
    # Duplicate original QR packets only in free physical display cells.
    assert sum(bool(label.pixmap() and not label.pixmap().isNull()) for label in window.machine_qrs) == window._OPTICAL_NORMAL_SLOTS
    window.close()
    app.processEvents()


def test_machine_mode_focus_command_never_promotes_inactive_window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()
    restore_calls = []
    monkeypatch.setattr(
        window.service.browser,
        "restore_easychange_foreground",
        lambda *args, **kwargs: restore_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(MainWindow, "isActiveWindow", lambda self: False)

    result = window.focus_command()

    assert restore_calls == []
    assert result["focus_restore_attempted"] is False
    assert result["focus_restore_skipped"] == "WINDOW_NOT_ACTIVE"
    assert result["user_focus_override"] is True
    window.close(); app.processEvents()


def test_machine_startup_enters_fullscreen_and_activates_command(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    calls = []
    scheduled = []
    monkeypatch.setattr(window, "showFullScreen", lambda: calls.append("fullscreen"))
    monkeypatch.setattr(window, "raise_", lambda: calls.append("raise"))
    monkeypatch.setattr(window, "activateWindow", lambda: calls.append("activate"))
    monkeypatch.setattr(
        main_window_module.QTimer, "singleShot",
        lambda delay, callback: scheduled.append((delay, callback)),
    )

    window._start_machine_display()

    assert calls == ["fullscreen", "raise", "activate"]
    assert window._startup_focus_pending is True
    assert len(scheduled) == 1 and scheduled[0][0] == 0
    window.close(); app.processEvents()


def test_machine_startup_retries_command_focus_until_confirmed(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    scheduled = []
    active_states = iter([False, True, True, True])
    focus_states = iter([False, False, True])
    focus_calls = []
    monkeypatch.setattr(window, "isActiveWindow", lambda: next(active_states))
    monkeypatch.setattr(window, "focus_command", lambda: focus_calls.append(True) or {})
    window.command = SimpleNamespace(hasFocus=lambda: next(focus_states))
    monkeypatch.setattr(
        main_window_module.QTimer, "singleShot",
        lambda delay, callback: scheduled.append((delay, callback)),
    )
    window._startup_focus_pending = True

    window._focus_command_at_startup()
    assert scheduled[-1][0] == window._STARTUP_FOCUS_RETRY_MS
    scheduled[-1][1]()
    assert scheduled[-1][0] == window._STARTUP_FOCUS_RETRY_MS
    scheduled[-1][1]()
    assert scheduled[-1][0] == window._STARTUP_FOCUS_RETRY_MS
    scheduled[-1][1]()

    assert focus_calls == [True, True, True]
    assert window._startup_focus_pending is False
    window.close(); app.processEvents()


def test_machine_mode_restore_callback_respects_minimized_window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()
    calls = []
    monkeypatch.setattr(MainWindow, "isMinimized", lambda self: True)
    monkeypatch.setattr(window, "focus_command", lambda: calls.append(True) or {})

    window._restore_command_focus()

    assert calls == []
    window.close(); app.processEvents()



def test_burst_header_remains_visible_on_every_page(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()
    window._optical_payloads = [f"payload-{index}" for index in range(65)]
    window._optical_header_payload = '{"p":"EC2H","s":"QPLAN","n":31}'
    window._optical_burst = True

    window._optical_page = 0
    slots, page_count, start, data_slots, include_header = window._optical_page_plan()
    assert slots == 32
    assert page_count == 3
    assert start == 0
    assert data_slots == 31
    assert include_header is True

    window._optical_page = 1
    slots, page_count, start, data_slots, include_header = window._optical_page_plan()
    assert slots == 32
    assert page_count == 3
    assert start == 31
    assert data_slots == 31
    assert include_header is True
    window.close(); app.processEvents()


def test_burst_rotation_dwell_exceeds_two_capture_periods(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()

    window._set_optical_burst(True)

    assert window._optical_timer.interval() == window._OPTICAL_BURST_INTERVAL_MS
    assert window._optical_timer.interval() >= 500
    window.close(); app.processEvents()


def test_large_burst_page_plan_covers_each_payload_exactly_once_per_cycle(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()
    window._optical_payloads = [f"payload-{index}" for index in range(80)]
    window._optical_header_payload = '{"p":"EC2H","s":"QPLAN2","n":80}'
    window._optical_burst = True

    seen = []
    window._optical_page = 0
    _, page_count, _, _, _ = window._optical_page_plan()
    for page in range(page_count):
        window._optical_page = page
        _slots, _count, start, data_slots, include_header = window._optical_page_plan()
        assert include_header is True
        seen.extend(range(start, min(start + data_slots, len(window._optical_payloads))))

    assert seen == list(range(len(window._optical_payloads)))
    window.close(); app.processEvents()


def test_explicit_human_gui_launch_resets_persisted_machine_transport(tmp_path):
    state_dir = tmp_path / ".easychange"
    state_dir.mkdir(parents=True)
    (state_dir / "session.json").write_text(
        json.dumps({
            "session_id": "SGUI",
            "workspace": str(tmp_path),
            "mode": "machine",
            "output_mode": "compact",
            "transport": "hid",
            "history": [],
            "aliases": {},
            "id_counts": {"C": 0, "R": 0},
            "macros": {},
            "snapshots": {},
            "file_refs": {},
            "results": {},
        }),
        encoding="utf-8",
    )

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=False, hid_mode=False)
    try:
        assert window._machine is False
        assert window.service.machine is False
        assert window.service.output == "text"
        assert window.service.state_store.data["mode"] == "human"
        assert window.service.state_store.data["transport"] == "local"
        assert window.service.state_store.data["output_mode"] == "text"
    finally:
        window.close(); app.processEvents()


def test_optical_render_is_non_reentrant_and_incremental(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()
    window._optical_payloads = [f"payload-{index}" for index in range(8)]
    window._optical_header_payload = '{"p":"EC2H","s":"QASYNC","n":8}'
    window._optical_burst = True
    builds = []
    original = window._build_optical_pixmap

    def counted(payload):
        builds.append(payload)
        return original(payload)

    monkeypatch.setattr(window, "_build_optical_pixmap", counted)
    window._render_optical_page()
    pending = window._optical_render_pending
    assert isinstance(pending, dict)
    assert builds == []
    window._render_optical_page()
    assert window._optical_render_pending is pending
    _drain_optical(window, app)
    assert len(builds) == len(set(builds))
    assert window._optical_render_pending is None
    window.close(); app.processEvents()



def test_optical_render_failure_does_not_leave_pending_ui_page(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    try:
        window._optical_payloads = ["invalid-qr"]
        window._optical_render_generation += 1
        window._render_optical_page()
        assert window._optical_render_pending is not None
        def invalid_qr(_):
            raise ValueError("QR_CAPACITY_EXCEEDED")
        monkeypatch.setattr(window, "_build_optical_pixmap", invalid_qr)
        window._render_next_optical_symbol()
        assert window._optical_render_pending is None
        assert window._optical_payloads == []
        assert "EC2 RENDER ERROR: ValueError" in window.optical_status.text()
        # The next valid page can start; failure does not poison the renderer.
        monkeypatch.undo()
        window._optical_payloads = ["valid-qr"]
        window._render_optical_page()
        _drain_optical(window, app)
        assert window.machine_qr.pixmap() is not None
    finally:
        window.close()
        app.processEvents()


def test_failed_optical_encoder_does_not_raise_in_gui_callback(tmp_path):
    from concurrent.futures import Future
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    try:
        future=Future()
        future.set_exception(OverflowError("ENVELOPE_TOO_LARGE"))
        window._optical_render_generation += 1
        generation=window._optical_render_generation
        window._optical_encode_future=future
        window._poll_optical_bundle(generation,future)
        assert window._optical_encode_future is None
        assert "EC2 ENCODE ERROR: OverflowError" in window.optical_status.text()
        assert window._optical_render_pending is None
    finally:
        window.close()
        app.processEvents()


def test_optical_transport_controls_are_explicit_and_drive_timer(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()

    config = window.service.configure_optical_transport(
        adaptive_enabled=True,
        min_ms=700,
        max_ms=1600,
        current_ms=1100,
        adjusted_by="test",
    )
    window._sync_optical_transport_controls()
    window._set_optical_burst(True)

    assert config["mode"] == "adaptive"
    assert window.optical_adaptive.isChecked() is True
    assert "MCP ajusta conforme HDMI/decoder" in window.optical_status.text()
    assert window.optical_min.value() == 700
    assert window.optical_current.value() == 1100
    assert window.optical_max.value() == 1600
    assert window._optical_timer.interval() == 1100

    window.close(); app.processEvents()



def test_machine_mode_large_result_broadcast_is_one_page():
    assert MainWindow._OPTICAL_BROADCAST_MAX_CHUNKS == 31
    assert MainWindow._OPTICAL_BURST_SLOTS == 32
    assert MainWindow._OPTICAL_BURST_COLUMNS == 8
    assert MainWindow._burst_qr_size_for_screen(1920, 1080) == 210
    assert 8 * 210 + 7 * 4 <= 1920 - 160
    assert 4 * 210 + 3 * 4 <= 1080 - 200



def test_optical_bundle_encoding_does_not_block_ui_thread(tmp_path, monkeypatch):
    import threading
    from easychange.core.result import Result

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    started = threading.Event()
    release = threading.Event()

    def slow_encode(result, **kwargs):
        started.set()
        assert release.wait(2.0)
        return (
            [
                json.dumps(
                    {"p": "EC2", "s": "QASYNC1", "i": 0, "n": 1, "d": "x"},
                    separators=(",", ":"),
                )
            ],
            '{"p":"EC2H","s":"QASYNC1","n":1}',
        )

    monkeypatch.setattr(
        "easychange.ui.main_window.encode_result_bundle",
        slow_encode,
    )

    window._update_machine_qr(
        Result(True, "study", {"x": "y" * 1000}, sequence="QASYNC1")
    )
    assert started.wait(1.0)
    assert window._optical_encode_future is not None
    assert window._optical_encode_future.done() is False
    assert window._optical_payloads == []

    # The UI thread is still free to process events while the worker is blocked.
    app.processEvents()
    assert window.command.isEnabled()

    release.set()
    _drain_optical(window, app)
    assert len(window._optical_payloads) == 1
    assert window.machine_qr.pixmap() is not None

    window.close()
    app.processEvents()



def test_optical_qr_size_switches_between_compact_and_burst(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    window.show(); app.processEvents()

    window._set_optical_burst(False)
    assert all(
        label.width() == window._OPTICAL_COMPACT_QR_SIZE
        and label.height() == window._OPTICAL_COMPACT_QR_SIZE
        for label in window.machine_qrs
    )

    window._set_optical_burst(True)
    available = window.screen().availableGeometry()
    burst_side = window._burst_qr_size_for_screen(available.width(), available.height())
    assert all(
        label.width() == burst_side and label.height() == burst_side
        for label in window.machine_qrs
    )
    window.close(); app.processEvents()



def test_human_gui_defers_file_tree_population_until_after_construction(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication
    from easychange.core.workspace import Workspace
    from easychange.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    calls = []
    monkeypatch.setattr(
        MainWindow,
        "_populate_tree",
        lambda self: calls.append("populate"),
    )
    window = MainWindow(Workspace.open(tmp_path), machine_mode=False, hid_mode=False)
    try:
        assert calls == []
        assert window.tree.topLevelItemCount() == 0
        window._populate_tree_deferred()
        assert calls == ["populate"]
    finally:
        window.close()
        app.processEvents()



def test_terminal_ec2h_is_visible_before_background_bundle_finishes(tmp_path, monkeypatch):
    import threading
    from easychange.core.result import Result

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Workspace.open(tmp_path), machine_mode=True, hid_mode=True)
    sequence = "QFASTHDR2"
    result = Result(True, "state", {"value": "ready"}, command_id="CFAST2", sequence=sequence)

    window.service.result_store.mark_running(
        sequence, "b" * 64, ":state", mutation=False
    )
    window.service.result_store.complete(
        sequence, "b" * 64, ":state", result.to_wire_dict()
    )

    started = threading.Event()
    release = threading.Event()

    def slow_encode(value, **kwargs):
        started.set()
        assert release.wait(2.0)
        return (
            ['{"p":"EC2","s":"QFASTHDR2","i":0,"n":1,"h":"x","c":"y","x":"z","d":"a"}'],
            '{"p":"EC2H","v":1,"s":"QFASTHDR2","f":true,"ok":true,"rc":null,"l":1,"n":1,"h":"x","m":null,"cid":"CFAST2","w":1}',
        )

    monkeypatch.setattr("easychange.ui.main_window.encode_result_bundle", slow_encode)
    window._update_machine_qr(result)

    assert started.wait(1.0)
    assert window._optical_encode_future is not None
    assert window._optical_encode_future.done() is False
    assert window.machine_qr.pixmap() is not None
    provisional = json.loads(window._optical_header_payload)
    assert provisional["p"] == "EC2H"
    assert provisional["s"] == sequence
    assert provisional["d"] == 1
    assert provisional["n"] == 0
    assert provisional["w"] == 0

    release.set()
    _drain_optical(window, app)
    assert len(window._optical_payloads) == 1
    assert window._optical_header_payload is None

    window.close()
    app.processEvents()

@pytest.mark.parametrize(
    "chunk_count,receipt_expected",
    [(1, False), (2, True), (3, True), (4, False)],
)
def test_compact_ec2_receipt_uses_spare_slot_without_extra_page(
    tmp_path, monkeypatch, chunk_count, receipt_expected,
):
    from easychange.core.result import Result

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        Workspace.open(tmp_path), machine_mode=True, hid_mode=True,
    )
    sequence = f"QSPARE{chunk_count}"
    payloads = [
        json.dumps(
            {"p": "EC2", "s": sequence, "i": i,
             "n": chunk_count, "d": f"fragment-{i}"},
            separators=(",", ":"),
        )
        for i in range(chunk_count)
    ]
    header = json.dumps(
        {"p": "EC2H", "s": sequence, "n": chunk_count},
        separators=(",", ":"),
    )
    monkeypatch.setattr(
        "easychange.ui.main_window.encode_result_bundle",
        lambda result, **kwargs: (list(payloads), header),
    )
    try:
        window._update_machine_qr(
            Result(True, "state", {"ready": True}, sequence=sequence)
        )
        _drain_optical(window, app)
        assert window._optical_burst is False
        assert bool(window._optical_header_payload) is receipt_expected
        slots, pages, start, data_slots, include_header = window._optical_page_plan()
        assert slots == 4
        assert pages == 1
        assert include_header is receipt_expected
        assert start == 0
        assert sum(
            label.pixmap() is not None and not label.pixmap().isNull()
            for label in window.machine_qrs
        ) == chunk_count + int(receipt_expected)
    finally:
        window.close()
        app.processEvents()



def test_optical_32_qr_canvas_fits_dpi_scaled_logical_screens():
    """All 32 cells must fit available geometry, even at 125-200% DPI."""
    assert MainWindow._burst_qr_size_for_screen(1920, 1080) == 210
    for width, height in ((1536, 864), (1280, 720), (960, 540), (640, 480)):
        side = MainWindow._burst_qr_size_for_screen(width, height)
        assert side > 0
        assert 8 * side + 7 * 4 <= max(0, width - 160)
        assert 4 * side + 3 * 4 <= max(0, height - 200)
    assert MainWindow._burst_qr_size_for_screen(1280, 720) == 127
