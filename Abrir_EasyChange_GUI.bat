@echo off
setlocal EnableExtensions

set "EASYCHANGE_ROOT=%~dp0"
set "EASYCHANGE_PY=%EASYCHANGE_ROOT%.venv\Scripts\python.exe"
set "EASYCHANGE_PYW=%EASYCHANGE_ROOT%.venv\Scripts\pythonw.exe"

if not exist "%EASYCHANGE_PY%" (
    echo [ERRO] Ambiente virtual do EasyChange nao foi encontrado:
    echo   %EASYCHANGE_PY%
    echo.
    echo Execute Atualizar_EasyChange.bat para criar ou reparar a .venv.
    pause
    exit /b 1
)

if not exist "%EASYCHANGE_PYW%" (
    echo [ERRO] pythonw.exe nao foi encontrado na .venv do EasyChange:
    echo   %EASYCHANGE_PYW%
    echo.
    echo Execute Atualizar_EasyChange.bat para reparar a .venv.
    pause
    exit /b 1
)

pushd "%EASYCHANGE_ROOT%"

rem Preflight with python.exe so import/startup errors are visible instead of
rem disappearing silently behind pythonw.exe.
"%EASYCHANGE_PY%" -c "import PySide6; import easychange.gui" >nul
if errorlevel 1 goto :startup_error

rem GUI means explicit HUMAN mode. Do not inherit MACHINE/HID/COMPACT from the
rem last remote-control session persisted in .easychange\session.json.
start "" "%EASYCHANGE_PYW%" -m easychange.gui "." --human
if errorlevel 1 goto :startup_error

popd
exit /b 0

:startup_error
echo.
echo [ERRO] Nao foi possivel iniciar a GUI do EasyChange.
echo Execute Atualizar_EasyChange.bat e tente novamente.
popd
pause
exit /b 2
