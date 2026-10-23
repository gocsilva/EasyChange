@echo off
setlocal
REM Full MCP tool engine + local Qt EasyChange + virtual HID/HDMI only.
set "MCP_SOURCE=%~dp0..\..\MCP_PC_CONTROLLER"
if not "%EASYCHANGE_VIRTUAL_MCP_SOURCE%"=="" set "MCP_SOURCE=%EASYCHANGE_VIRTUAL_MCP_SOURCE%"
set "PYTHON=%MCP_SOURCE%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo [FAIL] MCP virtual environment not found: %PYTHON%
  exit /b 2
)
pushd "%~dp0.."
"%PYTHON%" "%~dp0virtual_full_mcp_lab.py" --mcp-source "%MCP_SOURCE%" %*
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
