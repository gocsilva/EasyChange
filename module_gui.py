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
        data=json.loads(raw)
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

    def refresh(self):
        if self._closing:
            return
        def probe():
            report=status()
            if self._closing:
                return
            def update():
                if not self._closing:
                    self.message.set(
                        "Serviço ativo" if report["running"] else "Serviço parado")
            try:
                self.root.after(0,update)
            except (RuntimeError,tk.TclError):
                # Closing the GUI while health I/O is in flight is normal.
                pass
        threading.Thread(target=probe,daemon=True).start()

    def start(self):
        if self.process and self.process.poll() is None:
            return self.refresh()
        if status()["running"]:
            self.message.set("Serviço já iniciado fora desta GUI (mantido).")
            return
        folder=ROOT/"logs";folder.mkdir(exist_ok=True)
        python=ROOT/".venv"/"Scripts"/"python.exe"
        if not python.is_file():python=sys.executable
        opts={"creationflags":getattr(subprocess,"CREATE_NO_WINDOW",0)}
        if os.name=="nt":
            si=subprocess.STARTUPINFO()
            si.dwFlags|=subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow=getattr(subprocess,"SW_HIDE",0)
            opts["startupinfo"]=si
        with (folder/(MODULE_ID+"-gui-service.log")).open("ab") as output:
            self.process=subprocess.Popen([str(python),"-B",str(ROOT/SERVICE)],
                      cwd=ROOT,stdin=subprocess.DEVNULL,stdout=output,
                      stderr=subprocess.STDOUT,**opts)
        self.message.set("Serviço iniciando...")
        self.root.after(700,self.refresh)

    def stop(self):
        process=self.process
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:process.kill()
            self.message.set("Serviço iniciado por esta GUI foi encerrado.")
        else:
            self.message.set("Nenhum serviço próprio ativo. Outros processos preservados.")
        self.process=None

    def close(self):
        self._closing=True
        self.stop()
        self.root.destroy()

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
