# MCP integration

The optional stdio MCP server is implemented in `easychange/mcp/server.py` as a thin adapter around one `CommandService`. It is for workspaces local to MCP_HOST. Do not install or launch it on REMOTE_PC for B.M.O. control. Install the `[mcp]` extra only for a local workspace and start `python -m easychange.mcp.server <workspace>` there.

MCP server instructions bind the local workspace. It publishes `easychange://remote-guide` and `easychange://remote-profile` resources; those resources explicitly direct REMOTE_PC operation through ESP32 HID input and HDMI output only.

Tools include state, capabilities, a general EasyChange command tool, `remote_quickstart`, `prepare_hid_session`, `launch_hid_gui`, `workspace_brief`, `search_context`, `apply_edit_plan`, `verify_change`, file/read/search/edit, Git status/diff, build/test, argv-based process execution, undo, and transaction operations. The combined tools reduce network round trips: search plus context, workspace summary, change plan plus automatic rollback, or diff plus tests. The command tool accepts the CLI language, including chains and batches. `launch_hid_gui` starts a local GUI on the MCP host with Machine Mode and compact results; it reports the PySide6 install command when the optional GUI dependency is absent. Results use the shared structured envelope.

The HTTP API binds to loopback by default. It is not a REMOTE_PC channel; do not expose it to another machine.
