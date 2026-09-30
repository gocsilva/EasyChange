# EC1 sequenced HID commands

EasyChange accepts `:ec Q<id> <command>` as one keyboard line. The MCP appends Enter through the same TYPE_TEXT transfer. Compact Machine Mode output begins with `EC1 <SEQ>`, allowing the MCP_HOST to correlate the result read from HDMI.

The `.easychange/session.json` sequence journal stores up to 128 entries. A matching sequence and payload returns its cached result; a payload conflict returns `SEQUENCE_CONFLICT`; an unfinished entry returns `UNKNOWN`. The receiver writes `RUNNING` before dispatch and never repeats a mutation on its own.

Machine Mode restores focus to COMMAND. Ctrl+K remains the manual recovery shortcut. This protocol does not introduce a network API or remote filesystem channel.
