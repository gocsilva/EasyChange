"""Validate actual standalone GUI health function without importing tkinter."""
from __future__ import annotations

import ast
import io
import json
from pathlib import Path
from urllib.request import HTTPRedirectHandler


def _source_and_status():
    source=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    tree=ast.parse(source)
    method=next(node for node in tree.body
                if isinstance(node,ast.FunctionDef) and node.name=="status")
    name=next(node.value.value for node in tree.body
              if isinstance(node,ast.Assign)
              and any(isinstance(t,ast.Name) and t.id=="MODULE_ID"
                      for t in node.targets))
    port=next(node.value.value for node in tree.body
              if isinstance(node,ast.Assign)
              and any(isinstance(t,ast.Name) and t.id=="PORT"
                      for t in node.targets))
    return source,method,name,port


def _probe(method, name, port, payload):
    reads=[]
    class FakeResponse(io.BytesIO):
        def read(self,size=-1):
            reads.append(size)
            return super().read(size)
    response=FakeResponse(payload)
    namespace={"json":json,"MODULE_ID":name,"PORT":port,
               "urlopen":lambda url,timeout: response}
    exec(compile(ast.Module(body=[method],type_ignores=[]),
                 "<standalone-gui-health>","exec"),namespace)
    return namespace["status"](), reads


def test_health_uses_bounded_read_and_requires_own_module_identity():
    _src, method, name, port=_source_and_status()
    for payload,expected in (
        ({"ok":True,"id":name},True),
        ({"ok":True,"id":"unrelated"},False),
        ({"ok":1,"id":name},False),
        ([{"ok":True,"id":name}],False),
    ):
        result,reads=_probe(method,name,port,json.dumps(payload).encode())
        assert result["running"] is expected
        assert reads==[2049]
    large=b'{"ok":true,"id":"' + name.encode() + b'","pad":"' + b"x"*2100 + b'"}'
    result,reads=_probe(method,name,port,large)
    assert result["running"] is False
    assert reads==[2049]


def test_gui_local_opener_disables_proxy_and_redirects():
    source,method,name,port=_source_and_status()
    assert "build_opener(ProxyHandler({}), _NoStandaloneRedirect()).open" in source
    node=next(item for item in ast.parse(source).body
              if isinstance(item,ast.ClassDef)
              and item.name=="_NoStandaloneRedirect")
    namespace={"HTTPRedirectHandler":HTTPRedirectHandler}
    exec(compile(ast.Module(body=[node],type_ignores=[]),
                 "<no-redirect>","exec"),namespace)
    no_redirect=namespace["_NoStandaloneRedirect"]()
    assert no_redirect.redirect_request(None,None,None,None,None,None) is None


def test_health_refresh_does_not_update_destroyed_gui():
    import threading
    from unittest.mock import Mock

    source=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    node=next(item for item in ast.parse(source).body
              if isinstance(item,ast.ClassDef) and item.name=="ModuleGUI")

    class InstantThread:
        def __init__(self, *, target, daemon):
            self.target=target
        def start(self):
            self.target()

    namespace={"status":lambda:{"running":True},
               "threading":type("Threading",(),{"Thread":InstantThread}),
               "tk":type("Tk",(),{"TclError":RuntimeError})}
    exec(compile(ast.Module(body=[node],type_ignores=[]),
                 "<standalone-module-class>","exec"),namespace)
    gui=object.__new__(namespace["ModuleGUI"])
    gui.root=Mock()
    gui.message=Mock()
    gui.stop=Mock()
    gui._closing=False
    gui._refresh_inflight=False
    gui.refresh()
    gui.root.after.assert_called_once()
    callback=gui.root.after.call_args.args[1]
    gui.close()
    assert gui._closing is True
    gui.stop.assert_called_once()
    gui.root.destroy.assert_called_once()
    callback()
    gui.message.set.assert_not_called()

    gui.root.after.reset_mock()
    gui._closing=False
    def closing_during_probe():
        gui._closing=True
        return {"running":True}
    namespace["status"]=closing_during_probe
    gui.refresh()
    gui.root.after.assert_not_called()


def test_standalone_gui_launches_real_python_package_not_nested_script(tmp_path):
    import os
    import subprocess
    import sys
    from unittest.mock import Mock, patch

    source=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    tree=ast.parse(source)
    service=next(node.value.value for node in tree.body
                 if isinstance(node,ast.Assign) and any(
                     isinstance(t,ast.Name) and t.id=="SERVICE"
                     for t in node.targets))
    module_id=next(node.value.value for node in tree.body
                   if isinstance(node,ast.Assign) and any(
                       isinstance(t,ast.Name) and t.id=="MODULE_ID"
                       for t in node.targets))
    gui_class=next(node for node in tree.body
                   if isinstance(node,ast.ClassDef) and node.name=="ModuleGUI")
    namespace={"ROOT":tmp_path,"SERVICE":service,"MODULE_ID":module_id,
               "status":lambda:{"running":False},"subprocess":subprocess,
               "sys":sys,"os":os}
    exec(compile(ast.Module(body=[gui_class],type_ignores=[]),
                 "<standalone-package-start>","exec"),namespace)
    gui=object.__new__(namespace["ModuleGUI"])
    gui.process=None
    gui.message=Mock()
    gui.root=Mock()
    fake_process=Mock()
    with patch("subprocess.Popen",return_value=fake_process) as launched:
        gui.start()
    args=launched.call_args.args[0]
    assert args[1:]==["-B","-m",service[:-3].replace("/",".")]
    assert launched.call_args.kwargs["cwd"]==tmp_path
    assert gui.process is fake_process

    linux_python=tmp_path/".venv"/"bin"/"python"
    linux_python.parent.mkdir(parents=True)
    linux_python.write_bytes(b"")
    gui.process=None
    with patch("subprocess.Popen",return_value=fake_process) as launched_venv:
        gui.start()
    assert launched_venv.call_args.args[0][0]==str(linux_python)
    assert launched_venv.call_args.args[0][1:]==[
        "-B","-m",service[:-3].replace("/",".")]


def test_module_gui_startup_failures_stay_in_status_instead_of_raising(tmp_path):
    import os
    import subprocess
    import sys
    from unittest.mock import Mock,patch

    source=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    tree=ast.parse(source)
    assignments={target.id:node.value.value for node in tree.body
                 if isinstance(node,ast.Assign) and
                 isinstance(node.value,ast.Constant)
                 for target in node.targets if isinstance(target,ast.Name)}
    klass=next(item for item in tree.body
               if isinstance(item,ast.ClassDef) and item.name=="ModuleGUI")
    namespace={"ROOT":tmp_path,"SERVICE":assignments["SERVICE"],
               "MODULE_ID":assignments["MODULE_ID"],
               "status":lambda:{"running":False},
               "subprocess":subprocess,"os":os,"sys":sys}
    exec(compile(ast.Module(body=[klass],type_ignores=[]),
                 "<gui-service-launch>","exec"),namespace)
    gui=object.__new__(namespace["ModuleGUI"])
    gui.process=None
    gui.message=Mock()
    gui.root=Mock()
    with patch("subprocess.Popen",side_effect=OSError("BROKEN_INTERPRETER")):
        gui.start()
    assert gui.process is None
    assert "OSError" in gui.message.set.call_args.args[0]
    gui.message.reset_mock()
    with patch.object(Path,"mkdir",side_effect=PermissionError("LOG_DENIED")):
        gui.start()
    assert gui.process is None
    assert "PermissionError" in gui.message.set.call_args.args[0]


def test_repeated_gui_refreshes_are_single_flight(tmp_path):
    import threading
    from unittest.mock import Mock

    src=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    klass=next(node for node in ast.parse(src).body
               if isinstance(node,ast.ClassDef) and node.name=="ModuleGUI")
    deferred=[]
    class DeferredThread:
        def __init__(self,*,target,daemon):
            self.target=target
        def start(self):
            deferred.append(self.target)

    ns={"status":lambda:{"running":True},
        "threading":type("Deferred",(),{"Thread":DeferredThread}),
        "tk":type("Tk",(),{"TclError":RuntimeError})}
    exec(compile(ast.Module(body=[klass],type_ignores=[]),
                 "<gui-refresh>","exec"),ns)
    gui=object.__new__(ns["ModuleGUI"])
    gui.root=Mock()
    gui.message=Mock()
    gui._closing=False
    gui._refresh_inflight=False
    gui.refresh()
    for _ in range(100):
        gui.refresh()
    assert len(deferred)==1
    assert gui._refresh_inflight is True
    deferred.pop()()
    assert gui._refresh_inflight is False
    gui.refresh()
    assert len(deferred)==1
