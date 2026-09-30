param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace,
    [switch]$Human,
    [switch]$TextOutput
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "EasyChange virtual environment is missing under $repoRoot. Run: py -3 -m venv .venv, then .venv\Scripts\python.exe -m pip install -e '.[gui]'"
}
if (-not (Test-Path -LiteralPath $Workspace -PathType Container)) {
    throw "Workspace directory not found: $Workspace"
}

$arguments = @('-m', 'easychange.gui', (Resolve-Path -LiteralPath $Workspace).Path)
if (-not $Human) { $arguments += '--machine' }
if (-not $TextOutput) { $arguments += '--hid' }
& $python @arguments
exit $LASTEXITCODE
