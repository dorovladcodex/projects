param(
    [switch]$AllowDemoOrders
)

$ErrorActionPreference = "Stop"
if (-not $AllowDemoOrders) {
    throw "Explicit -AllowDemoOrders is required."
}

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$baseCanary = Join-Path $PSScriptRoot "bybit_demo_canary.ps1"

Push-Location $root
try {
    & $python ".\scripts\demo_kill_switch_diagnostics.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Read-only Demo diagnostics failed."
    }
    Write-Host "SIZING CANARY DIAGNOSTICS: PASS"

    & $python ".\scripts\demo_v2_preflight.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Read-only V2 preflight failed."
    }
    Write-Host "SIZING CANARY PREFLIGHT: PASS"

    & powershell -ExecutionPolicy Bypass -File $baseCanary `
        -Symbol XRPUSDT `
        -MaxNotionalUSDT 125 `
        -V2SizingTier 100 `
        -SkipControlledRestart `
        -AuthorizeCalculatedMinimumQuantity `
        -AllowDemoOrders
    if ($LASTEXITCODE -ne 0) {
        throw "100 USDT V2 sizing canary failed."
    }
    Write-Host "V2 100 USDT POSITION: PASS"

    & powershell -ExecutionPolicy Bypass -File $baseCanary `
        -Symbol SOLUSDT `
        -MaxNotionalUSDT 225 `
        -V2SizingTier 200 `
        -SkipControlledRestart `
        -AuthorizeCalculatedMinimumQuantity `
        -AllowDemoOrders
    if ($LASTEXITCODE -ne 0) {
        throw "200 USDT V2 sizing canary failed."
    }
    Write-Host "V2 200 USDT POSITION: PASS"
    Write-Host "V2 SIZING CANARY OVERALL: PASS"
}
finally {
    Pop-Location
}
