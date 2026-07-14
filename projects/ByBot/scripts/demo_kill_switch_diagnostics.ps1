$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    [Console]::Error.WriteLine("Local .venv Python is missing")
    exit 1
}
& $python (Join-Path $PSScriptRoot "demo_kill_switch_diagnostics.py")
exit $LASTEXITCODE
