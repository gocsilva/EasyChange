@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Atualizar EasyChange
cd /d "%~dp0"

where git >nul 2>nul
if errorlevel 1 (
    echo [ERRO] Git nao foi encontrado no PATH.
    pause
    exit /b 1
)

git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
    echo [ERRO] Esta pasta nao e um repositorio Git.
    pause
    exit /b 1
)

rem Nao sobrescreve alteracoes locais rastreadas.
git diff --quiet
if errorlevel 1 goto :local_changes

git diff --cached --quiet
if errorlevel 1 goto :local_changes

for /f %%V in ('git rev-parse HEAD') do set "OLD_VERSION=%%V"

echo Atualizando EasyChange pela branch main...
git fetch --prune origin main
if errorlevel 1 goto :git_error

git switch main
if errorlevel 1 goto :git_error

git pull --ff-only origin main
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

echo.
echo EasyChange atualizado com sucesso. Versao Git: %VERSION%
exit /b 0

:local_changes
echo.
echo [ERRO] Existem alteracoes locais rastreadas.
echo O atualizador nao vai sobrescrever seu trabalho.
echo Faca commit/stash antes de atualizar.
pause
exit /b 2

:python_error
echo.
echo [ERRO] O Git foi atualizado, mas nenhum Python 3.10+ funcional foi encontrado.
echo Instale/corrija o Python e execute Atualizar_EasyChange.bat novamente.
echo A .venv existente nao foi apagada.
pause
exit /b 5

:dependency_error
echo.
echo [ERRO] O Git foi atualizado, mas a criacao/reparo/sincronizacao da .venv falhou.
echo A atualizacao de codigo foi preservada e nenhum arquivo de projeto foi apagado.
echo Versao Git atual: %VERSION%
pause
exit /b 4

:git_error
echo.
echo [ERRO] Nao foi possivel atualizar o EasyChange.
echo Nenhuma alteracao local foi apagada.
pause
exit /b 3
