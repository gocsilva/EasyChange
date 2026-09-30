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
                assert {"state", "search", "edit_file", "undo", "command", "remote_quickstart", "prepare_hid_session", "launch_hid_gui"} <= names
                state = await session.call_tool("state", {})
                assert not state.is_error
                search = await session.call_tool("search", {"query": "find-me"})
                assert not search.is_error
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
