@echo off
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m easychange.gui
    exit /b 0
)

where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw -m easychange.gui
    exit /b 0
)

where python >nul 2>nul
if not errorlevel 1 (
    start "" python -m easychange.gui
    exit /b 0
)

echo [ERRO] Python nao foi encontrado.
pause
exit /b 1
