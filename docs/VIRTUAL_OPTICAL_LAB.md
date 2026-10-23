# EasyChange local virtual optical laboratory

This harness runs the real EasyChange MainWindow and CommandService against
a newly created temporary project. It is NOT another EasyChange app.

Virtual ESP32: PySide6 keyboard events to the *offscreen local* command
widget, never the physical USB-HID device.

Virtual HDMI: captures genuine Qt framebuffer using QWidget.grab and scales
it to 1280x720 with OpenCV, never opening VideoCapture or the capture card.

Virtual MCP: imports the real MCP optical decoder/focus matcher, applies
Tesseract to those frames, and checks sequence and CRC32.

Disposable project: TemporaryDirectory with two files, one small Python source
and one long UTF-8 document. Only read-only commands are executed.

No serial COM4, physical HDMI, BLE, network listener, tunnel or live MCP
server is started by the test harness.

## Run (on the local Windows PC)

From the EasyChange repository:

    tests\run_virtual_optical_lab.bat --max-cases 1
    tests\run_virtual_optical_lab.bat --max-cases 4

It uses the sibling MCP_PC_CONTROLLER venv and installed Tesseract.
Set EASYCHANGE_VIRTUAL_MCP_SOURCE to override the MCP source checkout.
Optional --evidence-dir C:\test-evidence writes virtual screenshots.

## Diagnostics

- direct_frame_crc_valid: exact Qt text decoded by production MCP, without OCR.
  Must be true to rule out protocol bugs.
- ocr_focus_valid: production focus recognizer applied to a 720p Qt capture.
- ocr_crc_valid: complete optical reading through Tesseract and CRC32.
  Failure MUST NOT be treated as success.
- preview_only: result is an explicit abbreviated receipt; recover via durable
  result protocol instead of treating a preview as complete.

First discovered bug: optical packet index regex accepted 00-03 but rejected
04-09 and higher. Regression verifies packets beyond index 10 and CRC safety.
Remaining concern: Base32 glyph recognition still fails in Tesseract at 720p.
The simulator reproduces this limitation without impacting remote operation.

No successful simulated test substitutes for physical HDMI testing later.


## Testing a different local EasyChange checkout

To avoid overwriting a locally modified official worktree, pass an explicit
source checkout. The harness imports the selected source *inside its own
subprocess*; neither the user's opened EasyChange nor live MCP is replaced.

    tests\run_virtual_optical_lab.bat --easychange-source C:\path\to\EasyChange --mcp-source C:\path\to\MCP_PC_CONTROLLER --max-cases 1

Use --start-case 3 --max-cases 2 for only the long-read and expected-error
tests. For local Windows this option is preferred when the main checkout has
uncommitted changes from concurrent development.

The launcher changes its process working directory to the repo root so Python
can import the actual EasyChange package, without requiring pip install.


## GitHub Actions: automatic updates from three mains

The reusable workflow is \`.github/workflows/virtual-optical-lab.yml\`.
GitHub Actions natively triggers it after pushes to **EasyChange/main**,
or manually from **EasyChange > Actions > EasyChange Virtual ESP-HDMI-MCP
Laboratory > Run workflow**. Each run retrieves the new EasyChange source
and creates a fresh offscreen instance; no running process is updated in
place.

Both private repositories \`gocsilva/MCP_PC_CONTROLLER\` and
\`gocsilva/ESP32-PC-CONTROLLER\` also install a small
\`.github/workflows/easychange-virtual-optical-lab.yml\` caller.
Any push to their respective \`main\` triggers the **same** reusable
workflow; its runner is attached to the **caller** private repository,
checks out that push's exact revision as \`component\`, and obtains the
latest *public* EasyChange test harness separately. This does **not**
commit a private MCP file to public EasyChange. All GitHub tokens are
read-only, and published test artifacts contain only synthetic workspaces'
screenshots and summaries.

For private MCP runs the lab imports the true MCP Ubuntu decoder and its
existing regression tests. For EasyChange and ESP runs the lab uses the
public self-contained wire-contract oracle; the ESP run additionally runs
serial-host unit tests. A virtual ESP keyboard never proves real firmware
behavior on the physical board.

A default run **fails** on missing/out-of-sequence/corrupted Qt receipts.
Optical OCR that cannot pass CRC32 is **reported as a warning** (it is
currently a known problem), never marked as a validated successful
delivery. Manual runs can enable \`strict_ocr\` to make any OCR failure
fail the workflow. Both modes upload a JSON result and synthetic
framebuffer captures for seven days, and add a summary to the Actions
run. No hardware or the local MCP/tunnel is involved.

The workflow updates its dependencies and fetches current sources on
each GitHub Actions run. It does **not** mutate or automatically publish
software fixes; source changes must be reviewed, committed and pushed
normally. Each trigger is a *new short-lived virtual test environment*
and does not overwrite the currently running local MCP GUI.

To add a fourth repository, create a read-only caller workflow analogous
to the MCP/ESP examples and define an appropriate kind/validator in the
reusable workflow before enabling it.


## Multi-mode read, write, search, and paired logs (October 23 audit)

The expanded local lab covers eight cases: state, small read, large read,
expected missing-file error, write to a disposable file, read-back, small
search, and paginated search. Only the temporary project is mutated.

Choose a mode with \`--transport-mode UBUNTU|QR|PIXEL|PIXEL+QR\`.
For QR/PIXEL/hybrid, supply \`--mcp-source\` to import the **real private**
MCP ZXing/PIXEL readers and EC2 fragment assembler. The collector observes
multiple GUI optical pages for up to eight seconds and requires sequence,
SHA-256 and CRC32 before reporting delivery. It never claims a missing
fragment as a successful result. This is a decoder integration test, **not**
a full \`easychange_exec\` MCP tool invocation with every production
permission/session preflight.

Example, on Windows in the EasyChange repository (for the current main
source checkouts):

    tests\run_virtual_optical_lab.bat --mcp-source C:\GPT_CONTROLLER_WORK\MCP_PC_CONTROLLER --transport-mode QR --start-case 7 --max-cases 1 --evidence-dir C:\temp\virtual-qr
    tests\run_virtual_optical_lab.bat --mcp-source C:\GPT_CONTROLLER_WORK\MCP_PC_CONTROLLER --transport-mode PIXEL --start-case 5 --max-cases 2 --evidence-dir C:\temp\virtual-pixel

The lab writes two **paired forensic journals** to the optional evidence
directory: \`easychange_transport_audit.jsonl\` (redacted sender-side
execution/encoding events) and \`mcp_virtual_receive.jsonl\`
(sequences that actually passed the receiver). It also captures one
virtual HDMI screenshot and the output pane text per case. These contain
only synthetic disposable project content. The EasyChange audit sink is
redirected to its disposable workspace instead of touching the real
\`LOCALAPPDATA\EasyChange\logs\`.

Observed:
- QR \`:state\` and \`:search\` returned complete, verified EC2 packets;
- PIXEL \`:state\` and a write receipt spanning two rotated pages were
  reconstructed with validated CRC;
- PIXEL+QR reassembled its mixed-fragment response;
- Ubuntu failed OCR CRC on synthetic HDMI captures, despite valid Qt text;
- 900-line reads could require 48-66 EC2 chunks, exceeding a short passive
  observation window, so full delivery requires the MCP durable-result
  header and logical-chunk recovery (not yet exercised end-to-end here).

An additional **real MCP session bug** was found: visual re-adoption
required a literal \`FOCUS: COMMAND\`, whereas HID preflight accepted the
safe 720p variation \`FOCUS: OMAND\` on a full matching EasyChange/MACHINE/HID
header. The private MCP change unifies both proofs with a regression test.

Default GitHub Actions now runs all eight synthetic cases. It records
the unresolved OCR issues instead of misreporting them as delivery.


## FULL MCP command/return pipeline — isolated local tests

The `tests/run_virtual_full_mcp_lab.bat` launcher calls the REAL
MCP `_send_and_correlate` against the REAL EasyChange Qt GUI in
a disposable subprocess using only synthetic hardware.

    tests\run_virtual_full_mcp_lab.bat --mode QR --command ":state --compact"
    tests\run_virtual_full_mcp_lab.bat --mode PIXEL --command ":write saved.txt SafeVirtualReplay" --replay-identical
    tests\run_virtual_full_mcp_lab.bat --mode QR --command ":read large.txt 1 900" --timeout 20

The controller's *device interfaces only* are replaced by virtual HID and
a condition-variable-fed 1280x720 Qt framebuffer. The GUI focus proof
is taken from virtual Qt widget state (not asserted to be physical OCR).
UBUNTU receipts invoke genuine Tesseract when selected. A production
permission modal and real device connectivity are outside this lab.

Security: a unique named optical OS mutex is created for each subprocess;
the host execution journal, LOCALAPPDATA and EasyChange audit logs use
TemporaryDirectory. All initial commands are **strictly allowlisted**:
state, disposable-source read and search, or write of safe ASCII test text
to the disposable `saved.txt`. No local/remote process commands,
admin operations, BLE, COM ports, capture cards or TCP listeners.

Each run checks the original remote execution occurred ONCE, that the MCP
returns a complete correctly sequenced response, and that any physical
write effects are exactly present on the temporary disk.
Large reads compare all 900 returned numbered lines to the source file.
`--replay-identical` repeats the SAME sequence and asserts the
host journal serves the second reply without another HID send.

The GitHub Actions reusable workflow runs state, search, PIXEL write plus
identical replay, hybrid search and 900-line durable recovery whenever
invoked from the private MCP repo. Public EasyChange does NOT access the
private source; no private code appears in public artifacts. A GitHub
Actions job requires eligible runners; creating the YAML trigger does not
guarantee a runner is allocated.

Results from the isolated simulation: QR, PIXEL and hybrid responses were
delivered with CRC/SHA verification for small tasks. A QR read of 900 lines
completed via multiple durable-result recovery HID requests with a SINGLE
original remote execution and correct content. No physical ESP/HDMI claims
are implied by these virtual successes.
