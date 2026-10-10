"""Standalone GUI must never spawn a competing service on an occupied port."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch


def _gui(tmp_path, *, healthy=False):
    tree=ast.parse((Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8"))
    settings={
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets if isinstance(target, ast.Name)
    }
    klass=next(n for n in tree.body if isinstance(n,ast.ClassDef)
               and n.name=="ModuleGUI")
    ns={"ROOT":tmp_path,"PORT":settings["PORT"],
        "MODULE_ID":settings["MODULE_ID"],"SERVICE":settings["SERVICE"],
        "status":lambda:{"running":healthy},"sys":sys,"os":os,
        "subprocess":subprocess}
    exec(compile(ast.Module(body=[klass],type_ignores=[]),
                 "<standalone-port-test>","exec"),ns)
    gui=object.__new__(ns["ModuleGUI"])
    gui.process=None
    gui.message=Mock()
    gui.root=Mock()
    return gui,settings


def test_bound_port_during_unhealthy_startup_does_not_spawn_competitor(tmp_path):
    gui,settings=_gui(tmp_path,healthy=False)
    connection=MagicMock()
    with patch("socket.create_connection",return_value=connection) as probe, \
         patch("subprocess.Popen") as spawn:
        gui.start()
    probe.assert_called_once_with(("127.0.0.1",settings["PORT"]),timeout=0.2)
    spawn.assert_not_called()
    assert gui.process is None
    assert "Porta local ocupada" in gui.message.set.call_args.args[0]


def test_healthy_external_service_is_preserved_without_even_probing_port(tmp_path):
    gui,_settings=_gui(tmp_path,healthy=True)
    with patch("socket.create_connection") as probe, \
         patch("subprocess.Popen") as spawn:
        gui.start()
    probe.assert_not_called()
    spawn.assert_not_called()
    assert gui.process is None
    assert "fora desta GUI" in gui.message.set.call_args.args[0]


def test_ambiguous_port_probe_failure_fails_closed(tmp_path):
    gui,_settings=_gui(tmp_path,healthy=False)
    for failure in (TimeoutError("PORT_PROBE_TIMED_OUT"),
                    PermissionError("SOCKET_DENIED"),
                    OSError("LOCAL_NETWORK_UNAVAILABLE")):
        with patch("socket.create_connection",side_effect=failure), \
             patch("subprocess.Popen") as spawn:
            gui.start()
        spawn.assert_not_called()
        assert gui.process is None
        assert "Não foi possível confirmar porta local" in gui.message.set.call_args.args[0]


def test_unbound_port_allows_normal_package_launch(tmp_path):
    gui,settings=_gui(tmp_path,healthy=False)
    process=Mock()
    process.pid=459
    with patch("socket.create_connection",side_effect=ConnectionRefusedError), \
         patch("subprocess.Popen",return_value=process) as spawn:
        gui.start()
    spawn.assert_called_once()
    assert spawn.call_args.args[0][1:] == [
        "-B","-m",settings["SERVICE"][:-3].replace("/",".")]
    assert gui.process is process
