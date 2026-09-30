@echo off
setlocal
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

if not "%OLD_VERSION%"=="%NEW_VERSION%" (
    git diff --name-only "%OLD_VERSION%" "%NEW_VERSION%" -- pyproject.toml | findstr /i /x "pyproject.toml" >nul
    if not errorlevel 1 (
        if exist ".venv\Scripts\python.exe" (
            echo Dependencias do EasyChange mudaram. Sincronizando .venv...
            ".venv\Scripts\python.exe" -m pip install -e ".[gui]"
            if errorlevel 1 goto :dependency_error
        )
    )
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

:dependency_error
echo.
echo [ERRO] O Git foi atualizado, mas a sincronizacao das dependencias falhou.
echo Versao Git atual: %VERSION%
pause
exit /b 4

:git_error
echo.
echo [ERRO] Nao foi possivel atualizar o EasyChange.
echo Nenhuma alteracao local foi apagada.
pause
exit /b 3
