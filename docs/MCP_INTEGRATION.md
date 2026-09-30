# MCP integration

The optional stdio MCP server is implemented in `easychange/mcp/server.py` as a thin adapter around one `CommandService`. Install the `[mcp]` extra and start `python -m easychange.mcp.server <workspace>`.

MCP exposes state, capabilities, a general EasyChange command tool, files/read/search/edit tools, Git status/diff, build/test, argv-based process execution, undo, and transaction operations. The general command tool accepts the same command language as the CLI, including chains and batches. Results use the shared structured result envelope.

The HTTP API binds to loopback by default. Add authentication and an explicit permission model before exposing destructive operations to another machine.
