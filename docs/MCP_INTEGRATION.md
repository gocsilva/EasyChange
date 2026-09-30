# MCP integration

The optional stdio MCP server is implemented in `easychange/mcp/server.py` as a thin adapter around one `CommandService`. Install the `[mcp]` extra and start `python -m easychange.mcp.server <workspace>`.

MCP server instructions tell the agent which workspace is bound and how to choose direct MCP, HDMI/HID, or Android relay operation. It publishes `easychange://remote-guide` and `easychange://remote-profile` resources so clients can discover the maintained procedure automatically.

Tools include state, capabilities, a general EasyChange command tool, `remote_quickstart`, `prepare_hid_session`, `launch_hid_gui`, file/read/search/edit, Git status/diff, build/test, argv-based process execution, undo, and transaction operations. The command tool accepts the CLI language, including chains and batches. `launch_hid_gui` starts a local GUI on the MCP host with Machine Mode and compact results; it reports the PySide6 install command when the optional GUI dependency is absent. Results use the shared structured envelope.

The HTTP API binds to loopback by default. Add authentication and an explicit permission model before exposing destructive operations to another machine.
