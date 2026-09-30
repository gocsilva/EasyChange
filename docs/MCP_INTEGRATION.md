# MCP integration point

The optional stdio MCP server is implemented in `easychange/mcp/server.py` as a thin adapter around one `CommandService` instance, not a second implementation of workspace operations. Install the `[mcp]` extra and start `python -m easychange.mcp.server <workspace>`.

Tools include state, capabilities, file listing/read/search/create/edit/replace, Git status/diff, build/test, argv-based command execution, undo, and transaction operations. Keep network transports local by default and add a permission model before allowing destructive operations remotely.
