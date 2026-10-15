## Módulo opcional MCP e GUI standalone

`start.bat` abre a GUI básica independente `module_gui.py`;
`Abrir_EasyChange_GUI.bat` continua disponível para a GUI
especializada. EasyChange não exige MCP nem ESP ao ser operado
externamente por sua própria interface.

Quando carregado **pelo MCP**, `module.mdl` inicia invisivelmente
`mcp_adapter/module_service.py` e o resolvedor ativa ESP. A
interface `module_bridge.py` permite que EasyChange consulte
as ferramentas do MCP e envie/receba mensagens de quaisquer
outros módulos habilitados. Operações remotas preservam o limite
HDMI + ESP/HID e não usam HTTP direto no PC remoto.

---

# EasyChange

EasyChange is a machine-first development workspace: a predictable command layer between an AI agent and a software workspace. The core works with any directory; project formats add optional build and test profiles.

## Install and run

Python 3.10 or newer is required. The core uses only the standard library.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,gui]"
python -m easychange C:\path\to\workspace
```

Run `easychange <workspace>` or `python -m easychange <workspace>` for the interactive CLI. Add `--json` for JSON output. Run the optional GUI with `python -m easychange.gui <workspace>`.

## Machine-first workflow

Commands accept a leading colon (optional in the CLI). Quoted arguments may contain spaces.

```text
:state
:files
:search NumeroProtocolo
:context R1
:replace-line src/example.py 12 "value = 2"
:diff && :test
:undo
```

The GUI uses **F12** for high-contrast Machine Mode, **Ctrl+K** to focus the command line, and **Esc** to return focus there. It includes line numbers, syntax highlighting, a persistent command field, and keyboard shortcuts. Use **Selecionar projeto…** in the header to open another folder in the same window. The GUI remembers the last successfully opened project in per-user application data and restores it when launched without an explicit workspace; an explicit workspace argument always takes precedence. Missing or corrupt remembered paths fall back to the current directory. On Windows, double-click `Abrir EasyChange.bat` to start the GUI; it restores the last available project or opens the EasyChange repository if none was saved.

Use `:capabilities` to discover commands. Features include indexed search, line-numbered reads, file editing and metadata, symbol definitions/references, batches and command chains, aliases/macros, snapshots, locks, transactions, persistent journal/undo/redo, Git status/diff/branch/log, process/build/test, and diagnostics. Runtime-aware commands add `:runtime-profile`, `:run-project`, `:test-smart`, `:swagger-evidence`, `:evidence`, `:process-logs`, `:db-connections`, `:db-schema`, and `:db-query`. They are optional capabilities: normal browsing/editing never depends on them. High-level commands include `:rename-symbol`, `:create-class`, `:create-interface`, and `:create-test`. Format/import commands report `ADAPTER_UNAVAILABLE` until a language adapter implements them.

Output can be changed with `:set output text|json|compact`; `:set transport hid` selects compact output. File paths are confined to the opened workspace. External processes run as argument arrays, without shell expansion.

## Interfaces

- **Machine GUI:** the production AI control surface. Commands enter only through keyboard/HID and results leave through the visible HDMI surface using compact EC1 text plus chunked EC2 optical QR packets.
- **CLI:** retained for local diagnostics, development, and automated tests.
- **No HTTP or MCP server is embedded in EasyChange.** B.M.O./MCP remains outside EasyChange and may interact with it only through the physical ESP32 HID + HDMI boundary.

## Adapters and persistence

Workspace detection recognizes Git, .NET, Python, Node, Rust, Go, Java, CMake, Unreal, and Godot markers without requiring them. Python, Node, and .NET adapters provide basic build/test profiles; unsupported build tools do not prevent generic browsing or editing.

Edits are recorded in a persistent `.easychange/` journal. Undo/redo checks file hashes; transactions can be recovered after restart. SQLite stores the incremental file index and cross-process locks. A generic regex provider supplies basic symbols and the provider interface can accept Tree-sitter or LSP integrations.

## B.M.O. and remote operation

Compact results, persistent IDs, ranked `:locate`, `:study`, `:read-many`, atomic batches, EC1 request sequences, EC2 chunked/compressed QR results, CRC/SHA verification, and the keyboard-focused Machine GUI reduce HDMI/HID round trips. Start the remote UI with `scripts/Start-EasyChangeRemote.ps1 -Workspace '<project path>'`. See [REMOTE_BOUNDARY.md](docs/REMOTE_BOUNDARY.md), [REMOTE_CONTROL.md](docs/REMOTE_CONTROL.md), [ANDROID_REMOTE.md](docs/ANDROID_REMOTE.md), [ESP32_HID_CONTRACT.md](docs/ESP32_HID_CONTRACT.md), [PROTOCOL.md](docs/PROTOCOL.md), and [ARCHITECTURE.md](docs/ARCHITECTURE.md).

The current repository does not include Tree-sitter/LSP servers, network control endpoints, or physical HDMI/ESP32 firmware. EasyChange itself stays transport-agnostic and visible; the external controller owns ESP32 HID and HDMI capture.

## Runtime, Swagger evidence, and databases

EasyChange can detect runnable/testable .NET API, worker, console, function and test projects, plus common Node, Python, Go, Rust, Maven and Gradle workspaces.

```text
:runtime-profile
:run-project
:test-smart
:swagger-evidence --profile dotnet:src/My.Api/My.Api.csproj
:evidence --title endpoint-smoke
```

Swagger evidence is saved under `.easychange/evidence/` and can include the HTTP response, process logs, a Markdown report, and a headless Edge/Chrome screenshot when a supported browser is installed.

SQL is **read-only by default**. SQLite can be queried directly by workspace-relative file name. SQL Server, PostgreSQL and MySQL use their local command-line clients when installed. Named connections live in `.easychange/db_connections.json`; passwords/tokens should be referenced through environment-variable names rather than stored in that file.

```json
{
  "connections": {
    "local-sqlserver": {
      "provider": "sqlserver",
      "server_env": "APP_DB_SERVER",
      "database_env": "APP_DB_NAME",
      "user_env": "APP_DB_USER",
      "password_env": "APP_DB_PASSWORD"
    }
  }
}
```

```text
:db-connections
:db-schema local-sqlserver
:db-query local-sqlserver "SELECT TOP 20 * FROM dbo.Example"
```

Mutating SQL requires an explicit `--write` flag. Database access, runtime execution, Swagger evidence, and all other advanced capabilities remain optional; EasyChange stays usable as a generic deterministic editor without them.

## Tests

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

## Ponte MCP opcional e mensagens confirmadas

EasyChange pode ser usado sem MCP, pela GUI própria. Quando ativado via `module.mdl`, a comunicação com outros módulos ocorre pela ponte central autenticada do MCP. `module_bridge.MCPBridge.receive()` lê sem remover; `ack(ids)` confirma somente mensagens processadas e `receive_legacy()` mantém compatibilidade antiga. As confirmações da ponte não substituem observação HDMI da operação física.

Persistência: quando carregado pelo MCP real, o spool SQLite local do MCP preserva mensagens não confirmadas após reinício; o teste sem spool é volátil. O destinatário deve processar reentregas de forma idempotente. O spool guarda payload em texto claro; não envie segredos por mensagens.

### Processamento seguro de mensagens do MCP

`module_bridge.MCPBridge.process_pending(handler, limit=20)` é **opt-in**: conectar este módulo não inicia consumo nem dispara ações. O callback é responsabilidade do módulo/operador; deve validar o tópico e o conteúdo antes de executar efeitos externos. O método lê a caixa via `GET /v1/inbox/peek` (não destrutivo), passa cada mensagem ao callback e só confirma cada ID por `POST /v1/inbox/ack` quando o callback retorna exatamente `True`. Ao retornar `False`/`None`, deixa a mensagem e as posteriores pendentes. Se houver exceção do callback, timeout ou ACK incerto, interrompe e propaga a falha sem confirmar a mensagem. Retorna `received`, `acknowledged` e `deferred_id`; limite de 1 a 20 mensagens por chamada. Ações não são executadas automaticamente, e os handlers precisam ser idempotentes: uma resposta de ACK perdida não prova que o efeito externo não ocorreu. `receive_legacy()` continua disponível por compatibilidade, mas é destrutivo e não deve ser utilizado para processamento confiável.

O código e os testes ficam nesta **única versão oficial do projeto na branch `main`**; o MCP permanece a ponte central e o módulo continua independente em modo standalone.

HTTP redirects and proxies are disabled for this local authenticated channel. Handlers must be idempotent across separate processes; the in-process consumer lock is not a distributed lock.

### ACK em lote opcional na ponte MCP

O `MCPBridge.process_pending(handler, limit=20, ack_batch_size=1)` continua confirmando **cada mensagem individualmente por padrao**. Para handlers explicitamente autorizados e idempotentes, `ack_batch_size=2..20` reduz chamadas HTTP e transacoes SQLite ao agrupar somente IDs cujos callbacks retornaram exatamente `True`. Em False ou excecao do handler, tenta confirmar apenas sucessos anteriores; em ACK desconhecido/contraditorio ou timeout, propaga a incerteza e nao reexecuta efeitos automaticamente. Um crash antes de gravar a confirmacao de lote pode reentregar todos os efeitos feitos desde o ultimo ACK; nunca use lote para comandos fisicos ou operacoes nao idempotentes sem garantias proprias de negocio.
