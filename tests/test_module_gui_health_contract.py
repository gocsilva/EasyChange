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
    gui._refresh_generation=0
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
    gui._refresh_generation=0
    gui.process=None
    gui.message=Mock()
    gui.root=Mock()
    fake_process=Mock()
    with patch("subprocess.Popen",return_value=fake_process) as launched, \
         patch("socket.create_connection",side_effect=ConnectionRefusedError):
        gui.start()
    args=launched.call_args.args[0]
    assert args[1:]==["-B","-m",service[:-3].replace("/",".")]
    assert launched.call_args.kwargs["cwd"]==tmp_path
    assert gui.process is fake_process

    linux_python=tmp_path/".venv"/"bin"/"python"
    linux_python.parent.mkdir(parents=True)
    linux_python.write_bytes(b"")
    gui.process=None
    with patch("subprocess.Popen",return_value=fake_process) as launched_venv, \
         patch("socket.create_connection",side_effect=ConnectionRefusedError):
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
    gui._refresh_generation=0
    gui.process=None
    gui.message=Mock()
    gui.root=Mock()
    with patch("subprocess.Popen",side_effect=OSError("BROKEN_INTERPRETER")), \
         patch("socket.create_connection",side_effect=ConnectionRefusedError):
        gui.start()
    assert gui.process is None
    assert "OSError" in gui.message.set.call_args.args[0]
    gui.message.reset_mock()
    with patch.object(Path,"mkdir",side_effect=PermissionError("LOG_DENIED")), \
         patch("socket.create_connection",side_effect=ConnectionRefusedError):
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
    gui._refresh_generation=0
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
    # Health I/O finished, but Tk has not yet consumed its queued result.
    assert gui._refresh_inflight is True
    for _ in range(100):
        gui.refresh()
    assert deferred==[]
    callback=gui.root.after.call_args.args[1]
    callback()
    assert gui._refresh_inflight is False
    gui.refresh()
    assert len(deferred)==1



def test_failed_health_probe_releases_single_flight_after_tk_update():
    from unittest.mock import Mock

    src=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    klass=next(node for node in ast.parse(src).body
               if isinstance(node,ast.ClassDef) and node.name=="ModuleGUI")
    class Immediate:
        def __init__(self,*,target,daemon):
            self.target=target
        def start(self):
            self.target()
    def unavailable():
        raise TimeoutError("SERVICE_DOWN")
    ns={"status":unavailable,
        "threading":type("Threading",(),{"Thread":Immediate}),
        "tk":type("Tk",(),{"TclError":RuntimeError})}
    exec(compile(ast.Module(body=[klass],type_ignores=[]),
                 "<gui-health-error>","exec"),ns)
    gui=object.__new__(ns["ModuleGUI"])
    gui._refresh_generation=0
    gui.root=Mock()
    gui.message=Mock()
    gui.process=None
    gui._closing=False
    gui._refresh_inflight=False
    gui.refresh()
    assert gui._refresh_inflight is True
    gui.refresh()
    gui.root.after.assert_called_once()
    callback=gui.root.after.call_args.args[1]
    callback()
    assert gui._refresh_inflight is False
    assert gui.message.set.call_args.args[0]=="Serviço parado"

def test_owned_process_is_reaped_and_failed_stop_keeps_gui_open():
    import subprocess
    from unittest.mock import Mock

    source=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    klass=next(node for node in ast.parse(source).body
               if isinstance(node,ast.ClassDef) and node.name=="ModuleGUI")
    ns={"subprocess":subprocess}
    exec(compile(ast.Module(body=[klass],type_ignores=[]),
                 "<module-process-stop>","exec"),ns)
    gui=object.__new__(ns["ModuleGUI"])
    gui._refresh_generation=0
    gui.root=Mock()
    gui.message=Mock()
    gui._closing=False
    owned=Mock()
    owned.poll.return_value=None
    owned.wait.side_effect=[subprocess.TimeoutExpired("module",2),0]
    gui.process=owned
    assert gui.stop() is True
    owned.terminate.assert_called_once()
    owned.kill.assert_called_once()
    assert owned.wait.call_count==2
    assert gui.process is None

    stuck=Mock()
    stuck.poll.return_value=None
    stuck.wait.side_effect=subprocess.TimeoutExpired("module",2)
    gui.process=stuck
    gui.close()
    assert gui._closing is False
    assert gui.process is stuck
    gui.root.destroy.assert_not_called()
    assert "Falha ao encerrar" in gui.message.set.call_args.args[0]


def test_owned_service_cold_start_keeps_polling_without_false_stopped_status():
    from unittest.mock import Mock

    source=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    klass=next(node for node in ast.parse(source).body
               if isinstance(node,ast.ClassDef) and node.name=="ModuleGUI")

    class ImmediateThread:
        def __init__(self,*,target,daemon):
            self.target=target
        def start(self):
            self.target()

    ns={"status":lambda:{"running":False},
        "threading":type("Async",(),{"Thread":ImmediateThread}),
        "tk":type("Tk",(),{"TclError":RuntimeError})}
    exec(compile(ast.Module(body=[klass],type_ignores=[]),
                 "<cold-start-poll>","exec"),ns)
    gui=object.__new__(ns["ModuleGUI"])
    gui._refresh_generation=0
    gui.root=Mock()
    gui.message=Mock()
    gui._closing=False
    gui._refresh_inflight=False
    gui.process=Mock()
    gui.process.poll.return_value=None

    gui.refresh()
    first=gui.root.after.call_args.args[1]
    first()
    assert "iniciando" in gui.message.set.call_args.args[0]
    delay,retry=gui.root.after.call_args.args
    assert delay==1600
    ns["status"]=lambda:{"running":True}
    retry()
    ready=gui.root.after.call_args.args[1]
    count=gui.root.after.call_count
    ready()
    assert gui.message.set.call_args.args[0]=="Serviço ativo"
    assert gui.root.after.call_count==count


def test_readiness_poll_is_single_timer_and_stops_after_ready_or_stop():
    """Repeated manual refreshes must not create repeated 1.6s poll chains."""
    from unittest.mock import Mock

    source=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    klass=next(item for item in ast.parse(source).body
               if isinstance(item,ast.ClassDef) and item.name=="ModuleGUI")

    class ImmediateThread:
        def __init__(self,*,target,daemon):
            self.target=target
        def start(self):
            self.target()

    class FakeTkRoot:
        def __init__(self):
            self.next_id=0
            self.pending={}
        def after(self,delay,callback):
            self.next_id+=1
            self.pending[self.next_id]=(delay,callback)
            return self.next_id
        def after_cancel(self,token):
            self.pending.pop(token,None)
        def fire(self,delay):
            token=next(k for k,v in self.pending.items() if v[0]==delay)
            _,callback=self.pending.pop(token)
            callback()

    ns={"status":lambda:{"running":False},
        "threading":type("Threading",(),{"Thread":ImmediateThread}),
        "tk":type("Tk",(),{"TclError":RuntimeError}),
        "subprocess":__import__("subprocess")}
    exec(compile(ast.Module(body=[klass],type_ignores=[]),
                 "<bounded-gui-readiness>","exec"),ns)
    gui=object.__new__(ns["ModuleGUI"])
    gui._refresh_generation=0
    gui.root=FakeTkRoot()
    gui.message=Mock()
    gui.process=Mock()
    gui.process.poll.return_value=None
    gui._closing=False
    gui._refresh_inflight=False
    gui._readiness_job=None

    gui.refresh()
    gui.root.fire(0)
    for _ in range(50):
        gui.refresh()
        gui.root.fire(0)
    assert sum(delay==1600 for delay,_ in gui.root.pending.values())==1

    ns["status"]=lambda:{"running":True}
    gui.refresh()
    gui.root.fire(0)
    assert gui.message.set.call_args.args[0]=="Serviço ativo"
    assert gui._readiness_job is None
    assert gui.root.pending=={}

    ns["status"]=lambda:{"running":False}
    gui.refresh()
    gui.root.fire(0)
    assert sum(delay==1600 for delay,_ in gui.root.pending.values())==1
    gui.stop()
    assert gui._readiness_job is None
    assert gui.root.pending=={}


def test_late_health_probe_or_queued_callback_cannot_revive_stopped_gui():
    """Two races: worker still running, or worker finished before Tk callback."""
    from unittest.mock import Mock

    source=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    klass=next(node for node in ast.parse(source).body
               if isinstance(node,ast.ClassDef) and node.name=="ModuleGUI")
    workers=[]
    class DeferredThread:
        def __init__(self,*,target,daemon):
            self.target=target
        def start(self):
            workers.append(self.target)

    namespace={
        "threading":type("Threads",(),{"Thread":DeferredThread}),
        "status":lambda:{"running":True},
        "tk":type("Tk",(),{"TclError":RuntimeError}),
    }
    exec(compile(ast.Module(body=[klass],type_ignores=[]),
                 "<stale-gui-health>","exec"),namespace)
    gui=object.__new__(namespace["ModuleGUI"])
    gui.root=Mock()
    gui.message=Mock()
    gui.process=None
    gui._closing=False
    gui._refresh_generation=0
    gui._refresh_inflight=False
    gui._readiness_job=None

    # Stopping during outstanding I/O: the old generation cannot schedule Tk.
    gui.refresh()
    assert len(workers)==1
    assert gui.stop() is True
    calls_after_stop=gui.message.set.call_count
    workers.pop()()
    assert gui.root.after.call_count==0
    assert gui.message.set.call_count==calls_after_stop

    # Stopping after I/O but before Tk executes its queued UI callback.
    gui.refresh()
    assert len(workers)==1
    workers.pop()()
    assert gui.root.after.call_count==1
    pending=gui.root.after.call_args.args[1]
    assert gui.stop() is True
    calls_after_stop=gui.message.set.call_count
    pending()
    assert gui.message.set.call_count==calls_after_stop
    assert gui._refresh_inflight is False


def test_stale_health_worker_cannot_clear_new_generation_inflight():
    """A stop/start race cannot let an obsolete /health steal the new slot."""
    from unittest.mock import Mock

    source=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(encoding="utf-8")
    cls=next(node for node in ast.parse(source).body
             if isinstance(node,ast.ClassDef) and node.name=="ModuleGUI")
    workers=[]

    class DeferredThread:
        def __init__(self,*,target,daemon):
            self.target=target
        def start(self):
            workers.append(self.target)

    env={"threading":type("Threads",(),{"Thread":DeferredThread}),
         "status":lambda:{"running":True},
         "tk":type("Tk",(),{"TclError":RuntimeError})}
    exec(compile(ast.Module(body=[cls],type_ignores=[]),
                 "<health-generation-race>","exec"),env)
    gui=object.__new__(env["ModuleGUI"])
    gui.root=Mock()
    gui.message=Mock()
    gui.process=None
    gui._closing=False
    gui._refresh_inflight=False
    gui._refresh_generation=0
    gui._readiness_job=None

    gui.refresh()
    old_worker=workers.pop()
    assert gui._refresh_inflight is True
    assert gui.stop() is True
    assert gui._refresh_generation==1
    assert gui._refresh_inflight is False

    gui.refresh()
    new_worker=workers.pop()
    assert gui._refresh_inflight is True
    old_worker()
    assert gui._refresh_inflight is True
    new_worker()
    assert gui._refresh_inflight is True
    gui.refresh()
    assert workers==[]
    gui.root.after.call_args.args[1]()
    assert gui._refresh_inflight is False
    assert gui.message.set.call_args.args[0]=="Serviço ativo"


def test_stale_queued_health_callback_cannot_release_new_probe():
    """Generation check must also guard the queued Tk finally block."""
    from unittest.mock import Mock

    source=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(encoding="utf-8")
    cls=next(node for node in ast.parse(source).body
             if isinstance(node,ast.ClassDef) and node.name=="ModuleGUI")
    workers=[]

    class DeferredThread:
        def __init__(self,*,target,daemon):
            self.target=target
        def start(self):
            workers.append(self.target)

    env={"threading":type("Threads",(),{"Thread":DeferredThread}),
         "status":lambda:{"running":True},
         "tk":type("Tk",(),{"TclError":RuntimeError})}
    exec(compile(ast.Module(body=[cls],type_ignores=[]),
                 "<health-queued-race>","exec"),env)
    gui=object.__new__(env["ModuleGUI"])
    gui.root=Mock()
    gui.message=Mock()
    gui.process=None
    gui._closing=False
    gui._refresh_inflight=False
    gui._refresh_generation=0
    gui._readiness_job=None

    gui.refresh()
    workers.pop()()
    old_callback=gui.root.after.call_args.args[1]
    assert gui.stop() is True
    gui.refresh()
    workers.pop()()
    new_callback=gui.root.after.call_args.args[1]
    old_callback()
    assert gui._refresh_inflight is True
    gui.refresh()
    assert workers==[]
    new_callback()
    assert gui._refresh_inflight is False


def test_standalone_child_cannot_inherit_mcp_host_capabilities(tmp_path):
    """Starting from an MCP parent must not transfer bearer or shutdown keys."""
    import os
    import subprocess
    import sys
    from unittest.mock import Mock, patch

    source=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    tree=ast.parse(source)
    assignments={target.id:node.value.value for node in tree.body
                 if isinstance(node,ast.Assign) and isinstance(node.value,ast.Constant)
                 for target in node.targets if isinstance(target,ast.Name)}
    cls=next(node for node in tree.body
             if isinstance(node,ast.ClassDef) and node.name=="ModuleGUI")
    env={"ROOT":tmp_path,"SERVICE":assignments["SERVICE"],
         "MODULE_ID":assignments["MODULE_ID"],
         "status":lambda:{"running":False},
         "os":os,"sys":sys,"subprocess":subprocess}
    exec(compile(ast.Module(body=[cls],type_ignores=[]),
                 "<module-standalone-child-env>","exec"),env)
    gui=object.__new__(env["ModuleGUI"])
    gui.process=None
    gui.root=Mock()
    gui.message=Mock()
    gui._closing=False
    gui._refresh_generation=0
    gui._refresh_inflight=False
    gui._readiness_job=None
    privileged={
        "MCP_MODULE_BUS_TOKEN":"sensitive-bus-token",
        "MCP_MODULE_BUS_URL":"http://127.0.0.1:8786",
        "MCP_MODULE_ID":"mcp-admin",
        "MCP_MODULE_GUI_SHUTDOWN_TOKEN":"sensitive-shutdown-token",
        "MODULE_STANDALONE_TEST_CONFIG":"allowed",
    }
    with patch.dict(os.environ,privileged), \
         patch("socket.create_connection",side_effect=ConnectionRefusedError), \
         patch("subprocess.Popen",return_value=Mock()) as launched:
        gui.start()
        child=launched.call_args.kwargs["env"]
        for secret in privileged:
            if secret.startswith("MCP_MODULE_"):
                assert secret not in child
                assert os.environ[secret]==privileged[secret]
        assert child["MODULE_STANDALONE_TEST_CONFIG"]=="allowed"
    assert gui.process is not None


def test_new_process_start_recovers_health_slot_from_old_stalled_probe(tmp_path):
    """A stale HTTP request cannot suppress readiness of the newly started PID."""
    import os
    import subprocess
    import sys
    from unittest.mock import Mock, patch

    src=(Path(__file__).resolve().parents[1]/"module_gui.py").read_text(
        encoding="utf-8")
    tree=ast.parse(src)
    constants={target.id:node.value.value for node in tree.body
               if isinstance(node,ast.Assign) and isinstance(node.value,ast.Constant)
               for target in node.targets if isinstance(target,ast.Name)}
    klass=next(node for node in tree.body
               if isinstance(node,ast.ClassDef) and node.name=="ModuleGUI")
    workers=[]
    class DeferredThread:
        def __init__(self,*,target,daemon):
            self.target=target
        def start(self):
            workers.append(self.target)

    ns={"ROOT":tmp_path,"SERVICE":constants["SERVICE"],
        "MODULE_ID":constants["MODULE_ID"],
        "status":lambda:{"running":True},
        "threading":type("Threads",(),{"Thread":DeferredThread}),
        "tk":type("Tk",(),{"TclError":RuntimeError}),
        "os":os,"sys":sys,"subprocess":subprocess}
    exec(compile(ast.Module(body=[klass],type_ignores=[]),
                 "<new-child-readiness>","exec"),ns)
    gui=object.__new__(ns["ModuleGUI"])
    gui.root=Mock()
    gui.message=Mock()
    gui.process=None
    gui._closing=False
    gui._refresh_inflight=False
    gui._refresh_generation=0
    gui._readiness_job=None

    gui.refresh()
    stale=workers.pop()
    with patch("socket.create_connection",side_effect=ConnectionRefusedError), \
         patch("subprocess.Popen",return_value=Mock()):
        gui.start()
    assert gui._refresh_generation==1
    assert gui._refresh_inflight is False
    gui.refresh()
    current=workers.pop()
    stale()
    assert gui._refresh_inflight is True
    current()
    assert gui._refresh_inflight is True
    gui.root.after.call_args.args[1]()
    assert gui._refresh_inflight is False
    assert gui.message.set.call_args.args[0]=="Serviço ativo"
