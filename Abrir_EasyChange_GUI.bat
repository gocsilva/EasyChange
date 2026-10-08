@echo off
setlocal EnableExtensions

set "EASYCHANGE_ROOT=%~dp0"
set "EASYCHANGE_PY=%EASYCHANGE_ROOT%.venv\Scripts\python.exe"

if not exist "%EASYCHANGE_PY%" (
    echo [ERRO] Ambiente virtual do EasyChange nao foi encontrado:
    echo   %EASYCHANGE_PY%
    echo.
    echo Execute Atualizar_EasyChange.bat para criar ou reparar a .venv.
    pause
    exit /b 1
)

pushd "%EASYCHANGE_ROOT%"

rem O launcher Python faz o handshake real da GUI:
rem - reutiliza/restaura uma janela EasyChange existente;
rem - evita abrir instancias duplicadas;
rem - inicia pythonw sem console somente quando necessario;
rem - espera uma janela EasyChange realmente existir;
rem - grava falhas em .easychange\gui-launch.log.
rem %~dp0 termina com barra invertida; depois do pushd use "." para evitar
rem que a barra final escape/mescle a aspa no argv do Windows.
"%EASYCHANGE_PY%" "scripts\launch_easychange_gui.py" "."
set "EASYCHANGE_EXIT=%ERRORLEVEL%"

if not "%EASYCHANGE_EXIT%"=="0" (
    echo.
    echo [ERRO] Nao foi possivel iniciar a GUI do EasyChange.
    echo Consulte .easychange\gui-launch.log para o erro real de inicializacao.
    pause
)

popd
exit /b %EASYCHANGE_EXIT%
