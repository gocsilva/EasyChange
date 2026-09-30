from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from easychange.core.command_service import CommandService
from easychange.core.workspace import Workspace
from easychange.remote.agent_profile import REMOTE_PROFILE, remote_guide_markdown, remote_quickstart as make_remote_quickstart


def create_server(workspace_path: str | Path = "."):
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise RuntimeError('MCP support is optional. Install with: pip install -e ".[mcp]"') from exc
    service = CommandService(Workspace.open(workspace_path))
    root = str(service.workspace.root_path)
    server = MCPServer(
        "EasyChange",
        instructions=(
            f"EasyChange is bound to workspace {root}. Start each task with state and capabilities. "
            "Use remote_guide for HDMI+ESP32 HID or Android relay operation. When MCP tools are connected, "
            "operate through them directly instead of OCR/HID. Keep paths in the bound workspace, inspect "
            "diffs, preserve returned IDs, and use undo/transactions for reversible edits. The stdio server "
            "is not network reachable."
        ),
    )
    server._easychange_service = service

    @server.resource("easychange://remote-guide", name="EasyChange remote guide", mime_type="text/markdown")
    def remote_guide() -> str:
        """Persistent instructions for MCP, HDMI/ESP HID, and Android relay clients."""
        return remote_guide_markdown()

    @server.resource("easychange://remote-profile", name="EasyChange remote profile", mime_type="application/json")
    def remote_profile() -> str:
        """Machine-readable startup, workflow, and transport guidance."""
        return json.dumps(REMOTE_PROFILE, ensure_ascii=False, separators=(",", ":"))

    @server.tool()
    def state() -> dict:
        """Return workspace state, detected adapters, branch, and transaction state."""
        return service.execute_tokens("state", []).to_dict()

    @server.tool()
    def capabilities() -> dict:
        """List supported EasyChange commands."""
        return service.execute_tokens("capabilities", []).data

    @server.tool()
    def remote_quickstart() -> dict:
        """Return ready-to-run GUI and MCP commands for this host and bound workspace."""
        return make_remote_quickstart(service.workspace.root_path, sys.executable)

    @server.tool()
    def prepare_hid_session() -> dict:
        """Set compact HID output and Machine Mode for the current EasyChange session."""
        result = service.execute(":prepare-hid")
        return {"result": result.to_dict(), "next": "Launch the GUI with --machine --hid, or focus its command field with Ctrl+K.",
                "workspace": root}

    @server.tool()
    def launch_hid_gui() -> dict:
        """Start the GUI on this host in Machine Mode with OCR-readable compact results."""
        if importlib.util.find_spec("PySide6") is None:
            return {"ok": False, "code": "GUI_DEPENDENCY_MISSING",
                    "setup": make_remote_quickstart(service.workspace.root_path, sys.executable)["gui_setup_command"]}
        process_id = service.ids.next("P")
        outcome = service.processes.start(
            [sys.executable, "-m", "easychange.gui", root, "--machine", "--hid"], process_id
        )
        service._persist_state()
        return {"ok": True, **outcome, "workspace": root, "focus_shortcut": "Ctrl+K"}

    @server.tool()
    def command(text: str) -> dict:
        """Execute one EasyChange command or a bounded command chain and return a structured result."""
        return service.execute(text).to_dict()

    @server.tool()
    def list_files(limit: int = 500) -> dict:
        """List workspace files with short stable IDs."""
        return service.execute_tokens("files", [str(limit)]).data

    @server.tool()
    def read_file(path: str, start: int = 1, count: int = 120) -> dict:
        """Read line-numbered text from a workspace-relative path or F/R ID."""
        return service.execute_tokens("read", [path, str(start), str(count)]).to_dict()

    @server.tool()
    def search(query: str, limit: int = 100) -> dict:
        """Search workspace text and return short result IDs."""
        return service.execute_tokens("search", [query, str(limit)]).to_dict()

    @server.tool()
    def create_file(path: str, content: str = "") -> dict:
        """Create a text file inside the workspace."""
        result = service.execute_tokens("new", [path])
        if result.ok and content:
            result = service.execute_tokens("write", [path, content])
        return result.to_dict()

    @server.tool()
    def edit_file(path: str, content: str) -> dict:
        """Replace a workspace text file with new content; the edit is journaled and undoable."""
        return service.execute_tokens("write", [path, content]).to_dict()

    @server.tool()
    def replace(path: str, old: str, new: str) -> dict:
        """Replace matching text in a workspace file."""
        return service.execute_tokens("replace", [path, old, new]).to_dict()

    @server.tool()
    def git_status() -> dict:
        """Return Git branch and changed paths."""
        return service.execute_tokens("status", []).to_dict()

    @server.tool()
    def git_diff() -> dict:
        """Return the unstaged Git diff."""
        return service.execute_tokens("diff", []).to_dict()

    @server.tool()
    def build() -> dict:
        """Run the detected build profile, if available."""
        return service.execute_tokens("build", []).to_dict()

    @server.tool()
    def test() -> dict:
        """Run the detected test profile, if available."""
        return service.execute_tokens("test", []).to_dict()

    @server.tool()
    def execute_command(argv: list[str]) -> dict:
        """Execute a program as an argv list in the workspace, without shell expansion."""
        return service.execute_tokens("run", argv).to_dict()

    @server.tool()
    def undo() -> dict:
        """Undo the most recent committed workspace text edit."""
        return service.execute_tokens("undo", []).to_dict()

    @server.tool()
    def transaction_begin() -> dict:
        """Start a group of reversible edits."""
        return service.execute_tokens("begin", []).to_dict()

    @server.tool()
    def transaction_commit() -> dict:
        """Commit buffered transaction edits into the undo journal."""
        return service.execute_tokens("commit", []).to_dict()

    @server.tool()
    def transaction_rollback() -> dict:
        """Restore files edited since transaction_begin."""
        return service.execute_tokens("rollback", []).to_dict()

    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="EasyChange MCP server over stdio")
    parser.add_argument("workspace", nargs="?", default=".")
    args = parser.parse_args()
    try:
        server = create_server(args.workspace)
    except (RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        asyncio.run(server.run_stdio_async())
    finally:
        server._easychange_service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
