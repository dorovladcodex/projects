param(
    [switch]$AllowDemoOrders
)

$ErrorActionPreference = "Stop"
if (-not $AllowDemoOrders) {
    throw "Explicit -AllowDemoOrders is required."
}

$root = Split-Path -Parent $PSScriptRoot
$baseCanary = Join-Path $PSScriptRoot "bybit_demo_canary.ps1"

Push-Location $root
try {
    & powershell -ExecutionPolicy Bypass -File $baseCanary `
        -Symbol XRPUSDT `
        -MaxNotionalUSDT 125 `
        -V2SizingTier 100 `
        -ExerciseDepthGate `
        -SkipControlledRestart `
        -AuthorizeCalculatedMinimumQuantity `
        -AllowDemoOrders
    if ($LASTEXITCODE -ne 0) {
        throw "Targeted V2 depth-gate canary failed."
    }
    Write-Host "DEPTH GATE STAGE B TRADE: PASS"
    Write-Host "DEPTH GATE CANARY OVERALL: PASS"
}
finally {
    Pop-Location
}
