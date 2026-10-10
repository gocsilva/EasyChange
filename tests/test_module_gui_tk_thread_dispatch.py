"""Standalone module Tk updates must only be dispatched by the GUI thread."""
from __future__ import annotations

import ast
import queue
import types
from pathlib import Path
from unittest.mock import Mock


class InlineThread:
    def __init__(self, *, target, daemon):
        self.target=target
    def start(self):
        self.target()


def _headless_gui():
    source=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    klass=next(node for node in ast.parse(source).body
               if isinstance(node,ast.ClassDef) and node.name=="ModuleGUI")
    namespace={
        "queue":queue,
        "tk":types.SimpleNamespace(TclError=RuntimeError),
        "threading":types.SimpleNamespace(Thread=InlineThread),
        "status":lambda:{"running":True},
    }
    exec(compile(ast.Module(body=[klass],type_ignores=[]),
                 "<standalone-tk-dispatch>","exec"),namespace)
    gui=object.__new__(namespace["ModuleGUI"])
    gui.root=Mock()
    gui.message=Mock()
    gui.process=None
    gui._closing=False
    gui._refresh_generation=0
    gui._refresh_inflight=False
    gui._readiness_job=None
    gui._gui_results=queue.Queue(maxsize=16)
    gui._gui_pump_job=None
    return gui


def test_worker_only_queues_result_until_tk_pumps_it():
    gui=_headless_gui()
    gui.refresh()
    # Worker has completed, but must not invoke Tk APIs directly.
    gui.root.after.assert_not_called()
    gui.message.set.assert_not_called()
    assert gui._refresh_inflight is True
    assert gui._gui_results.qsize()==1

    gui._drain_gui_results()
    gui.message.set.assert_called_once_with("Serviço ativo")
    assert gui._refresh_inflight is False
    gui.root.after.assert_called_once()
    assert gui.root.after.call_args.args[0]==75


def test_result_queue_has_memory_bound_and_closed_ui_drops_new_results():
    gui=_headless_gui()
    gui._gui_results=queue.Queue(maxsize=1)
    fn=Mock()
    assert gui._post_gui_result(fn) is True
    assert gui._post_gui_result(Mock()) is False
    assert gui._gui_results.qsize()==1
    gui._closing=True
    assert gui._post_gui_result(Mock()) is False
    gui._drain_gui_results()
    fn.assert_not_called()
    gui.root.after.assert_not_called()


def test_destroy_cancels_gui_owned_dispatch_timer():
    gui=_headless_gui()
    gui._gui_pump_job="active-pump"
    gui.stop=Mock(return_value=True)
    gui.close()
    gui.root.after_cancel.assert_called_once_with("active-pump")
    gui.root.destroy.assert_called_once()
