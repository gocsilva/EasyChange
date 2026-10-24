@echo off
setlocal
REM ALWAYS fetch latest EasyChange/MCP/ESP remote main before virtual tests.
REM Worktrees are disposable; the live GUI, ESP COM, HDMI, tunnel are untouched.
set "MCP_HOME=%~dp0..\..\MCP_PC_CONTROLLER"
set "PYTHON=%MCP_HOME%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo [FAIL] MCP Python venv missing: %PYTHON%
  exit /b 2
)
pushd "%~dp0.."
"%PYTHON%" "%~dp0fresh_main_virtual_lab.py" --lab-kind full %*
set "LAB_RESULT=%ERRORLEVEL%"
popd
exit /b %LAB_RESULT%
