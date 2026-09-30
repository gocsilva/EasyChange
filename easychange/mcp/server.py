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
            "This optional MCP adapter is for a workspace local to MCP_HOST only. Never run or connect it on "
            "REMOTE_PC for B.M.O. control. For REMOTE_PC, use the EasyChange GUI through ESP32 HID input and "
            "HDMI output exclusively. Use remote_guide for the physical workflow. Keep paths in the bound workspace, inspect "
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
    def search_context(query: str, limit: int = 5, context_lines: int = 2,
                       extension: str = "", path_prefix: str = "") -> dict:
        """Find matches and attach nearby line-numbered context in one round trip."""
        args = [query, "--limit", str(max(1, min(10, limit))),
                "--context", str(max(0, min(20, context_lines)))]
        if extension: args.append("ext:" + extension)
        if path_prefix: args.append("path:" + path_prefix)
        return service.execute_tokens("locate", args).to_dict()

    @server.tool()
    def workspace_brief(file_limit: int = 20) -> dict:
        """Return state, capabilities, and a short file page as one compact workspace snapshot."""
        state_result = service.execute_tokens("state", [])
        capability_result = service.execute_tokens("capabilities", [])
        files_result = service.execute_tokens("files", [str(max(1, min(100, file_limit)))])
        return {"state": state_result.data, "capabilities": capability_result.data,
                "files": files_result.data}

    @server.tool()
    def apply_edit_plan(steps: list[dict]) -> dict:
        """Apply a bounded edit plan as one undoable transaction; roll back the whole plan on the first error."""
        if not steps or len(steps) > 50:
            return {"ok": False, "code": "INVALID_PLAN", "message": "Provide between 1 and 50 edit steps."}
        if service.transaction is not None:
            return {"ok": False, "code": "TRANSACTION_ACTIVE", "message": "Finish the active transaction first."}
        started = service.execute_tokens("begin", [])
        if not started.ok: return {"ok": False, "code": started.code, "message": started.error}
        operation_map = {"write": ("write", ("path", "content")),
                         "replace": ("replace", ("path", "old", "new")),
                         "replace_line": ("replace-line", ("path", "line", "text")),
                         "replace_range": ("replace-range", ("path", "start", "end", "text")),
                         "insert": ("insert", ("path", "line", "text")),
                         "delete": ("delete", ("path",))}
        results = []
        for index, step in enumerate(steps, 1):
            operation = operation_map.get(str(step.get("operation", "")))
            if operation is None:
                failure = {"ok": False, "code": "INVALID_PLAN", "error": f"Unsupported operation at step {index}."}
                results.append(failure)
                break
            command_name, fields = operation
            try:
                values = [str(step[field]) for field in fields]
            except KeyError as exc:
                failure = {"ok": False, "code": "INVALID_PLAN", "error": f"Missing field at step {index}: {exc.args[0]}"}
                results.append(failure)
                break
            result = service.execute_tokens(command_name, values)
            results.append(result.to_dict())
            if not result.ok: break
        failed = next((index for index, result in enumerate(results) if not result["ok"]), None)
        if failed is not None:
            rollback = service.execute_tokens("rollback", [])
            return {"ok": False, "failed_step": failed + 1, "steps": results, "rollback": rollback.to_dict()}
        commit = service.execute_tokens("commit", [])
        return {"ok": commit.ok, "transaction": started.data.get("transaction"),
                "steps": results, "commit": commit.to_dict()}

    @server.tool()
    def verify_change() -> dict:
        """Run Git diff followed by the detected test profile as one MCP round trip."""
        diff_result = service.execute_tokens("diff", [])
        test_result = service.execute_tokens("test", [])
        return {"ok": diff_result.ok and test_result.ok,
                "diff": diff_result.to_dict(), "test": test_result.to_dict()}

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
