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
"%EASYCHANGE_PYTHON%" -m easychange.gui "%EASYCHANGE_ROOT%" --machine --hid
set "EASYCHANGE_EXIT=%ERRORLEVEL%"
popd
exit /b %EASYCHANGE_EXIT%
