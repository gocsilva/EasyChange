import asyncio
import sys

import pytest

mcp = pytest.importorskip("mcp")
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def test_stdio_mcp_tools(tmp_path):
    (tmp_path / "sample.txt").write_text("find-me", encoding="utf-8")

    async def exercise():
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "easychange.mcp.server", str(tmp_path)])
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                assert "remote_guide" in (initialized.instructions or "")
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {"state", "search", "edit_file", "undo", "command", "remote_quickstart", "prepare_hid_session", "launch_hid_gui",
                        "workspace_brief", "search_context", "apply_edit_plan", "verify_change"} <= names
                state = await session.call_tool("state", {})
                assert not state.is_error
                search = await session.call_tool("search", {"query": "find-me"})
                assert not search.is_error
                with_context = await session.call_tool("search_context", {"query": "find-me", "context_lines": 0})
                assert not with_context.is_error
                brief = await session.call_tool("workspace_brief", {"file_limit": 5})
                assert not brief.is_error
                edited = await session.call_tool("apply_edit_plan", {"steps": [
                    {"operation": "replace_line", "path": "sample.txt", "line": 1, "text": "changed"}
                ]})
                assert not edited.is_error and (tmp_path / "sample.txt").read_text(encoding="utf-8") == "changed"
                rolled_back = await session.call_tool("apply_edit_plan", {"steps": [
                    {"operation": "replace_line", "path": "sample.txt", "line": 1, "text": "temporary"},
                    {"operation": "replace_line", "path": "sample.txt", "line": 99, "text": "bad"},
                ]})
                assert not rolled_back.is_error and (tmp_path / "sample.txt").read_text(encoding="utf-8") == "changed"
                capabilities = await session.call_tool("command", {"text": ":capabilities"})
                assert not capabilities.is_error
                quickstart = await session.call_tool("remote_quickstart", {})
                assert not quickstart.is_error
                prepared = await session.call_tool("prepare_hid_session", {})
                assert not prepared.is_error
                resources = await session.list_resources()
                uris = {str(resource.uri) for resource in resources.resources}
                assert {"easychange://remote-guide", "easychange://remote-profile"} <= uris
                guide = await session.read_resource("easychange://remote-guide")
                assert "HDMI" in guide.contents[0].text and "Android" in guide.contents[0].text

    asyncio.run(exercise())
