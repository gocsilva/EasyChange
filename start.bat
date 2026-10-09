@echo off
setlocal
cd /d "%~dp0"
rem Default: basic independent GUI; --service starts headless module service.
if exist ".venv\Scripts\python.exe" (
  set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3 is required.
    exit /b 1
  )
  set "PYTHON_EXE=python"
)
if /i "%~1"=="--service" (
  "%PYTHON_EXE%" -B mcp_adapter/module_service.py %2 %3 %4
  exit /b %errorlevel%
)
"%PYTHON_EXE%" -B module_gui.py %*
exit /b %errorlevel%
