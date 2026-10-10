"""Standalone GUI start must never block Tk on HTTP /health."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock, Mock, patch

import pytest


def _isolated_gui(tmp_path):
    source = (Path(__file__).resolve().parents[1] / "module_gui.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    klass = next(node for node in tree.body
                 if isinstance(node, ast.ClassDef) and node.name == "ModuleGUI")
    assignments = {
        target.id: node.value.value
        for node in tree.body if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        for target in node.targets if isinstance(target, ast.Name)
    }
    def forbidden_health():
        raise AssertionError("SYNCHRONOUS_HEALTH_CALLED_IN_TK")
    globals_ = {
        "ROOT": tmp_path, "MODULE_ID": assignments["MODULE_ID"],
        "SERVICE": assignments["SERVICE"], "PORT": assignments["PORT"],
        "status": forbidden_health, "subprocess": subprocess, "sys": sys,
        "os": os,
    }
    exec(compile(ast.Module(body=[klass],type_ignores=[]),
                 "<standalone-gui-start>", "exec"), globals_)
    gui = object.__new__(globals_["ModuleGUI"])
    gui.process = None
    gui.message = Mock()
    gui.refresh = Mock()
    return gui, assignments


def test_occupied_local_port_never_spawns_or_blocks_on_http(tmp_path):
    gui, config = _isolated_gui(tmp_path)
    with patch("socket.create_connection", return_value=MagicMock()) as port, \
         patch("subprocess.Popen", side_effect=AssertionError("DUPLICATE_PROCESS")):
        gui.start()
    port.assert_called_once_with(("127.0.0.1", config["PORT"]), timeout=0.2)
    gui.refresh.assert_called_once()
    assert gui.process is None
    assert "Porta local ocupada" in gui.message.set.call_args.args[0]


def test_refused_port_can_spawn_without_waiting_for_http_health(tmp_path):
    gui, config = _isolated_gui(tmp_path)
    spawned = Mock(pid=1234)
    with patch("socket.create_connection",
               side_effect=ConnectionRefusedError("PORT_FREE")) as port, \
         patch("subprocess.Popen", return_value=spawned) as launch:
        gui.start()
    port.assert_called_once_with(("127.0.0.1", config["PORT"]), timeout=0.2)
    assert launch.call_count == 1
    assert launch.call_args.args[0][-2:] == [
        "-m", config["SERVICE"][:-3].replace("/", ".")]
    assert gui.process is spawned


def test_inconclusive_port_probe_cannot_spawn_duplicate_service(tmp_path):
    gui, _config = _isolated_gui(tmp_path)
    with patch("socket.create_connection", side_effect=TimeoutError("UNCERTAIN")), \
         patch("subprocess.Popen", side_effect=AssertionError("DUPLICATE_PROCESS")) as launch:
        gui.start()
    launch.assert_not_called()
    assert gui.process is None
    assert "TimeoutError" in gui.message.set.call_args.args[0]
