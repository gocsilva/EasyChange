# Machine protocol

The transport-neutral core accepts a command string or already-tokenized command and returns a structured `Result`:

```json
{"ok":true,"command":"search","data":{"matches":[{"id":"R1","file":"src/a.py","line":8,"text":"NumeroProtocolo"}]},"error":null,"code":null,"command_id":"C2","duration_ms":3}
```

Text output uses stable `KEY: VALUE` headers and pipe-delimited rows. Compact mode emits one line: `OK <command-id> <command> <duration>ms <data-json>` or `ERR <command-id> <code> <command> <error-json>`. The JSON payload keeps result IDs and error details available to OCR without dumping pretty-printed output. JSON output is selected with `:set output json`; `:set transport hid` selects compact output.

`easychange.remote.machine_protocol` accepts a JSON object with a non-empty `command` string and encodes one compact JSON result per line. Workspace (`W1`), file (`F...`), search (`R...`), and command (`C...`) references are short. File and search result IDs are resolved by command operations such as `:read R1` and `:open F1`. ID counters, session ID, aliases, recent history/results, and current file are persisted under `.easychange/` for the workspace.

Command parsing supports quoted arguments, `;` chains, `&&` stop-on-error chains, and `:batch ... :end`. Example: `:replace-line "src/file with spaces.py" 12 "updated = True"`. Paths are workspace-relative by default and may not escape the selected root.

CLI, API, MCP, and GUI adapters call the same command service. The current protocol is local and unauthenticated; the HTTP server binds to loopback unless deliberately changed in code. Do not expose it to a network before adding authentication and permissions.
