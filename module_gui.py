"""Minimal GUI for independent {{NAME}}. Never requires the MCP host."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk
from urllib.request import ProxyHandler, HTTPRedirectHandler, build_opener

class _NoStandaloneRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

# A GUI health check must stay on localhost, without inherited proxy settings.
urlopen = build_opener(ProxyHandler({}), _NoStandaloneRedirect()).open

ROOT=Path(__file__).resolve().parent
NAME="EasyChange"
MODULE_ID="easychange"
PORT=8784
SERVICE="mcp_adapter/module_service.py"

def status():
    try:
        with urlopen(f"http://127.0.0.1:{PORT}/health",timeout=0.6) as r:
            raw=r.read(2049)
        if len(raw)>2048:
            raise ValueError("MODULE_GUI_HEALTH_TOO_LARGE")
        # Health is an identity proof. Ambiguous duplicate fields or
        # nonstandard/nonfinite JSON must not turn a foreign local listener
        # into an apparently healthy module.
        def unique_fields(pairs):
            result={}
            for key,value in pairs:
                if key in result:
                    raise ValueError("MODULE_GUI_DUPLICATE_HEALTH_FIELD")
                result[key]=value
            return result
        def finite_float(token):
            number=float(token)
            if not -float("inf") < number < float("inf"):
                raise ValueError("MODULE_GUI_NONFINITE_HEALTH")
            return number
        data=json.loads(
            raw, object_pairs_hook=unique_fields, parse_float=finite_float,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("MODULE_GUI_NONFINITE_HEALTH")))
        return {"running":isinstance(data,dict) and
                data.get("ok") is True and data.get("id")==MODULE_ID,
                "service":MODULE_ID,"standalone":True}
    except Exception:
        return {"running":False,"service":MODULE_ID,"standalone":True}

def self_test():
    return {"module":MODULE_ID,"mcp_required":False,"gui":"standalone",
            "service":SERVICE,"manifest":"module.mdl"}

class ModuleGUI:
    def __init__(self):
        self.root=tk.Tk()
        self.root.title(NAME+" — independente")
        self.root.geometry("510x255")
        self.process=None
        self._closing=False
        self._refresh_inflight=False
        self._refresh_generation=0
        self._readiness_job=None
        parent=ttk.Frame(self.root,padding=18)
        parent.pack(fill="both",expand=True)
        ttk.Label(parent,text=NAME,font=("Segoe UI",17,"bold")).pack(anchor="w")
        ttk.Label(parent,text="Modo independente • sem MCP • sem outros módulos",
                  wraplength=470).pack(anchor="w",pady=(6,12))
        row=ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Button(row,text="Iniciar",command=self.start).pack(side="left",padx=(0,8))
        ttk.Button(row,text="Parar",command=self.stop).pack(side="left",padx=(0,8))
        ttk.Button(row,text="Atualizar",command=self.refresh).pack(side="left")
        self.message=tk.StringVar(value="Verificando serviço...")
        ttk.Label(parent,textvariable=self.message,wraplength=460).pack(anchor="w",pady=12)
        self.root.protocol("WM_DELETE_WINDOW",self.close)
        self.refresh()

    def _cancel_readiness(self):
        pending=getattr(self,"_readiness_job",None)
        self._readiness_job=None
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except (RuntimeError,tk.TclError):
                pass

    def _readiness_tick(self):
        self._readiness_job=None
        if not self._closing:
            self.refresh()

    def refresh(self):
        if self._closing or self._refresh_inflight:
            return
        self._refresh_inflight=True
        generation=self._refresh_generation
        def probe():
            try:
                report=status()
            except Exception:
                report={"running":False}
            if self._closing or generation!=self._refresh_generation:
                # start()/stop() has invalidated this probe and may already
                # own a newer one. Never clear that newer probe's slot.
                return
            def update():
                try:
                    if self._closing or generation!=self._refresh_generation:
                        return
                    owned=self.process
                    if (not report["running"] and owned is not None
                            and owned.poll() is None):
                        self.message.set("Serviço iniciando; aguardando /health...")
                        # Repeated manual refreshes must not multiply the
                        # long-poll timers during a slow module startup.
                        if getattr(self,"_readiness_job",None) is None:
                            self._readiness_job=self.root.after(
                                1600,self._readiness_tick)
                    else:
                        self._cancel_readiness()
                        self.message.set(
                            "Serviço ativo" if report["running"] else "Serviço parado")
                finally:
                    # The worker is not done until Tk has consumed its result:
                    # releasing here (not after I/O) prevents callback storms.
                    if generation==self._refresh_generation:
                        self._refresh_inflight=False
            try:
                self.root.after(0,update)
            except (RuntimeError,tk.TclError):
                # A closing Tk interpreter cannot receive the callback.
                if generation==self._refresh_generation:
                    self._refresh_inflight=False
        try:
            threading.Thread(target=probe,daemon=True).start()
        except RuntimeError:
            if generation==self._refresh_generation:
                self._refresh_inflight=False
            self.message.set("Não foi possível iniciar verificação de saúde.")

    def start(self):
        if self.process is not None:
            try:
                if self.process.poll() is None:
                    return self.refresh()
            except (OSError,ValueError) as exc:
                # Unknown PID state must never authorize a second standalone
                # service that may contest the same port or physical device.
                self.message.set("Estado do serviço desconhecido: "+type(exc).__name__)
                return
        # Keep the Tk callback nonblocking: /health can stall for 600ms.
        # The fixed-port ownership check below is fail-closed; the regular
        # background refresh reports whether an external listener is healthy.
        # An external instance may be bound but still initializing or hung.
        # Never spawn a second owner of this module's device/port merely
        # because its HTTP /health has not answered yet.
        import socket
        try:
            with socket.create_connection(("127.0.0.1",PORT),timeout=0.2):
                self.message.set(
                    "Porta local ocupada; verificando serviço externo.")
                self.refresh()
                return
        except ConnectionRefusedError:
            # Explicitly refused: no listener currently owns the local port.
            pass
        except OSError as exc:
            # A timeout or socket/resource error is not proof the port is free.
            self.message.set(
                "Não foi possível confirmar porta local: "+type(exc).__name__)
            return
        # Invalidate an older asynchronous /health result before spawning.
        self._refresh_generation+=1
        # An older /health request must not block readiness for this new PID.
        self._refresh_inflight=False
        self._cancel_readiness()
        folder=ROOT/"logs"
        python=ROOT/".venv"/"Scripts"/"python.exe"
        if not python.is_file():
            python=ROOT/".venv"/"bin"/"python"
        if not python.is_file():
            python=sys.executable
        opts={"creationflags":getattr(subprocess,"CREATE_NO_WINDOW",0)}
        if os.name=="nt":
            si=subprocess.STARTUPINFO()
            si.dwFlags|=subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow=getattr(subprocess,"SW_HIDE",0)
            opts["startupinfo"]=si
        # Execute the installed package from the repository root; running a
        # nested .py file directly breaks absolute package imports on Python.
        module_entry=SERVICE[:-3].replace("/",".")
        # Independent GUI services must never inherit a privileged MCP-host
        # session or shutdown capability from their parent process.
        child_env=os.environ.copy()
        for key in ("MCP_MODULE_BUS_TOKEN","MCP_MODULE_BUS_URL",
                    "MCP_MODULE_ID","MCP_MODULE_GUI_SHUTDOWN_TOKEN"):
            child_env.pop(key,None)
        try:
            folder.mkdir(exist_ok=True)
            with (folder/(MODULE_ID+"-gui-service.log")).open("ab") as output:
                self.process=subprocess.Popen([str(python),"-B","-m",module_entry],
                          cwd=ROOT,stdin=subprocess.DEVNULL,stdout=output,
                          stderr=subprocess.STDOUT,env=child_env,**opts)
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            # A missing/broken interpreter is a service startup failure, not
            # an uncaught Tk callback that makes the GUI appear frozen.
            self.message.set("Falha ao iniciar serviço: "+type(exc).__name__)
            return
        self.message.set("Serviço iniciando...")
        self.root.after(700,self.refresh)

    def stop(self):
        # A late worker/callback cannot resurrect the state of a stopped PID.
        self._refresh_generation+=1
        self._refresh_inflight=False
        self._cancel_readiness()
        process=self.process
        try:
            running=process is not None and process.poll() is None
        except (OSError,ValueError) as exc:
            # An invalid process handle is not proof that it exited. Keep
            # ownership, so close/restart cannot orphan or duplicate it.
            self.message.set("Falha ao consultar serviço: "+type(exc).__name__)
            return False
        if running:
            try:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    # Reap the process after forceful termination as well.
                    process.wait(timeout=2)
            except (OSError,subprocess.TimeoutExpired) as exc:
                try:
                    still_running=process.poll() is None
                except (OSError,ValueError):
                    # Unknown outcome: never claim release of the owned PID.
                    still_running=True
                if still_running:
                    self.message.set("Falha ao encerrar serviço: "+type(exc).__name__)
                    return False
            self.message.set("Serviço iniciado por esta GUI foi encerrado.")
        else:
            self.message.set("Nenhum serviço próprio ativo. Outros processos preservados.")
        self.process=None
        return True

    def close(self):
        self._closing=True
        self._cancel_readiness()
        if self.stop():
            self.root.destroy()
        else:
            # Preserve ownership and let the user retry stopping the service.
            self._closing=False

    def run(self):
        self.root.mainloop()

def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--self-test",action="store_true")
    args=parser.parse_args(argv)
    if args.self_test:
        print(json.dumps(self_test(),ensure_ascii=False));return 0
    ModuleGUI().run()
    return 0

if __name__=="__main__":
    raise SystemExit(main())
