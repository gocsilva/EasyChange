@echo off
setlocal
REM Virtual Qt / HID / HDMI / MCP. No serial, camera, tunnel or MCP listener.
set "MCP_SOURCE=%~dp0..\..\MCP_PC_CONTROLLER"
if not "%EASYCHANGE_VIRTUAL_MCP_SOURCE%"=="" set "MCP_SOURCE=%EASYCHANGE_VIRTUAL_MCP_SOURCE%"
set "PYTHON=%MCP_SOURCE%\.venv\Scripts\python.exe"
set "TESSERACT=C:\Program Files\Tesseract-OCR\tesseract.exe"
if not exist "%PYTHON%" (
  echo [FAIL] MCP Python environment missing: %PYTHON%
  exit /b 2
)
if not exist "%TESSERACT%" (
  echo [FAIL] Tesseract missing: %TESSERACT%
  exit /b 3
)
"%PYTHON%" "%~dp0virtual_optical_lab.py" --mcp-source "%MCP_SOURCE%" --tesseract "%TESSERACT%" %*
exit /b %ERRORLEVEL%
