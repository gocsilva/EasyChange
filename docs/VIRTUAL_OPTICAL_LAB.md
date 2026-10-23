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
