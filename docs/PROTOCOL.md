# Machine protocol

The transport-neutral core accepts one command string at a time and returns a structured `Result`:

```json
{"ok":true,"command":"search","data":{"matches":[{"id":"R1","file":"src/a.py","line":8,"text":"NumeroProtocolo"}]},"error":null,"code":null,"command_id":"C2","duration_ms":3}
```

Text output uses stable `KEY: VALUE` headers and pipe-delimited result rows. Compact mode emits `OK <command-id> <command> <duration>ms` (or `ERR <code> <message>`). JSON mode is selected with `:set output json`.

`easychange.remote.machine_protocol` accepts a JSON object with a non-empty `command` string and serializes a result as a single compact JSON line. IDs are short within a process (`W1`, `F1`, `R1`, `C1`); file IDs are presentation references and current commands accept paths, while search IDs are included for clients to map to paths. A future protocol revision should add direct ID resolution, session IDs, versioning, and persistent state before remote compatibility is promised.

Command parsing uses shell-like quoting. Example: `:replace-line "src/file with spaces.py" 12 "updated = True"`. Paths are resolved relative to the selected workspace and must remain within it.
