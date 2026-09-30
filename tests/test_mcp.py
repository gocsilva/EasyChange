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
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {"state", "search", "edit_file", "undo", "command"} <= names
                state = await session.call_tool("state", {})
                assert not state.is_error
                search = await session.call_tool("search", {"query": "find-me"})
                assert not search.is_error
                capabilities = await session.call_tool("command", {"text": ":capabilities"})
                assert not capabilities.is_error

    asyncio.run(exercise())
