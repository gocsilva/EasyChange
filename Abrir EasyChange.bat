@echo off
setlocal

set "EASYCHANGE_ROOT=%~dp0"
set "EASYCHANGE_PYTHON=%EASYCHANGE_ROOT%.venv\Scripts\python.exe"

if not exist "%EASYCHANGE_PYTHON%" (
    echo EasyChange virtual environment not found at:
    echo   %EASYCHANGE_PYTHON%
    echo Create it with: py -3 -m venv .venv
    echo Then install the GUI with: .venv\Scripts\python.exe -m pip install -e ".[gui]"
    pause
    exit /b 1
)

pushd "%EASYCHANGE_ROOT%"
rem IMPORTANT: %~dp0 ends with a backslash. Passing it quoted as the last
rem character of a Windows argv value can escape/merge the closing quote,
rem causing "--machine --hid" to become part of the workspace path.
rem pushd already places us at the workspace, so pass "." as a separate argv.
"%EASYCHANGE_PYTHON%" -m easychange.gui --machine --hid
set "EASYCHANGE_EXIT=%ERRORLEVEL%"
popd
exit /b %EASYCHANGE_EXIT%
