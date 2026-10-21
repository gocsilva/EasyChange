@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Atualizar EasyChange
cd /d "%~dp0"

where git >nul 2>nul
if errorlevel 1 (
    echo [ERRO] Git nao foi encontrado no PATH.
    call :pause_if_interactive
    exit /b 1
)

git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
    echo [ERRO] Esta pasta nao e um repositorio Git.
    call :pause_if_interactive
    exit /b 1
)

rem Refuse every local change, including staged and untracked files.
for /f "delims=" %%S in ('git status --porcelain --untracked-files=all') do set "LOCAL_CHANGE=1"
if defined LOCAL_CHANGE goto :local_changes

for /f "delims=" %%B in ('git branch --show-current') do set "CURRENT_BRANCH=%%B"
if /i not "%CURRENT_BRANCH%"=="main" goto :wrong_branch

for /f %%V in ('git rev-parse HEAD') do set "OLD_VERSION=%%V"

echo Atualizando EasyChange pela branch main...
git fetch --prune origin main
if errorlevel 1 goto :git_error

git merge-base --is-ancestor "%OLD_VERSION%" origin/main
if errorlevel 1 goto :diverged_history

git merge --ff-only origin/main
if errorlevel 1 goto :git_error

for /f %%V in ('git rev-parse HEAD') do set "NEW_VERSION=%%V"
for /f %%V in ('git rev-parse --short HEAD') do set "VERSION=%%V"

echo.
echo Verificando se o ambiente virtual .venv precisa ser criado, reparado ou atualizado...

set "BOOTSTRAP_PY="
set "VENV_PY=%CD%\.venv\Scripts\python.exe"

rem Prefere um Python real fora da .venv para conseguir recriar o ambiente se necessario.
for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined BOOTSTRAP_PY (
        set "CANDIDATE=%%~fP"
        if /i not "!CANDIDATE!"=="!VENV_PY!" (
            "!CANDIDATE!" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
            if not errorlevel 1 set "BOOTSTRAP_PY=!CANDIDATE!"
        )
    )
)

rem Se nao houver Python no PATH, uma .venv saudavel ainda pode sincronizar dependencias.
if not defined BOOTSTRAP_PY if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 set "BOOTSTRAP_PY=%VENV_PY%"
)

if defined BOOTSTRAP_PY (
    "%BOOTSTRAP_PY%" "scripts\ensure_venv.py"
    if errorlevel 1 goto :dependency_error
) else (
    rem Ultimo fallback: Python Launcher do Windows, se estiver funcional.
    where py >nul 2>nul
    if errorlevel 1 goto :python_error
    py -3 "scripts\ensure_venv.py"
    if errorlevel 1 goto :dependency_error
)

"%VENV_PY%" -c "from easychange import __version__; from easychange.remote.optical_protocol import PROTOCOL; assert PROTOCOL == 'EC2'; print('EASYCHANGE_ACTIVE=' + __version__ + ' PROTOCOL=' + PROTOCOL)"
if errorlevel 1 goto :dependency_error

echo.
echo EasyChange atualizado com sucesso. Versao Git: %VERSION%
exit /b 0

:local_changes
echo.
echo [ERRO] Existem alteracoes locais, staged ou nao rastreadas.
echo O atualizador nao vai sobrescrever seu trabalho.
echo Salve seu trabalho e execute a atualizacao novamente.
call :pause_if_interactive
exit /b 2

:wrong_branch
echo.
echo [ERRO] O atualizador exige a branch main e nao vai trocar sua branch ativa.
call :pause_if_interactive
exit /b 2

:diverged_history
echo.
echo [ERRO] A main local nao e ancestral da origin/main; atualizacao recusada.
echo O atualizador nao vai descartar commits locais nem reescrever historico.
call :pause_if_interactive
exit /b 3

:python_error
echo.
echo [ERRO] Nenhum Python 3.10+ funcional foi encontrado.
echo Instale/corrija o Python e execute Atualizar_EasyChange.bat novamente.
echo A .venv existente nao foi apagada.
call :rollback_update
call :pause_if_interactive
exit /b 5

:dependency_error
echo.
echo [ERRO] A criacao/reparo/sincronizacao da .venv ou a verificacao do protocolo falhou.
call :rollback_update
call :pause_if_interactive
exit /b 4

:rollback_update
git status --porcelain --untracked-files=all >nul
if errorlevel 1 (
    echo [AVISO] Nao foi possivel verificar o estado para rollback; codigo permanece em %VERSION%.
    exit /b 0
)
for /f "delims=" %%S in ('git status --porcelain --untracked-files=all') do set "ROLLBACK_BLOCKED=1"
if defined ROLLBACK_BLOCKED (
    echo [AVISO] Arquivos locais mudaram durante a atualizacao; rollback recusado para preserva-los.
    echo Versao Git atual: %VERSION%
    exit /b 0
)
git reset --keep "%OLD_VERSION%"
if errorlevel 1 (
    echo [AVISO] Rollback Git falhou; codigo permanece em %VERSION%.
    exit /b 0
)
echo Rollback Git concluido para %OLD_VERSION%.
echo Dependencias da .venv nao sao revertidas automaticamente; execute o atualizador apos corrigir o erro.
exit /b 0

:pause_if_interactive
if not defined EASYCHANGE_NONINTERACTIVE pause
exit /b 0

:git_error
echo.
echo [ERRO] Nao foi possivel atualizar o EasyChange.
echo Nenhuma alteracao local foi apagada.
call :pause_if_interactive
exit /b 3
