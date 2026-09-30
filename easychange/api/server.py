from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace


class CommandBody(BaseModel):
    command: str


def create_app(workspace_path: str | Path = ".") -> FastAPI:
    service = CommandService(Workspace.open(workspace_path))
    app = FastAPI(title="EasyChange API", version="0.1.0")

    @app.get("/api/state")
    def state(): return service.execute(":state").to_dict()

    @app.get("/api/capabilities")
    def capabilities(): return service.execute(":capabilities").to_dict()

    @app.post("/api/command")
    def command(body: CommandBody):
        result = service.execute(body.command)
        if not result.ok: raise HTTPException(status_code=400, detail=result.to_dict())
        return result.to_dict()

    @app.post("/api/search")
    def search(body: CommandBody): return service.execute(":search " + body.command).to_dict()

    return app


def main() -> None:
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(); parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("EasyChange API binds to loopback only")
    uvicorn.run(create_app(args.workspace), host=args.host, port=args.port)
