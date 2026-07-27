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
    & $python ".\scripts\demo_v2_network_fault_canary.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Deterministic network-degradation canary failed."
    }
    & powershell -ExecutionPolicy Bypass -File $baseCanary `
        -Symbol XRPUSDT `
        -MaxNotionalUSDT 125 `
        -V2SizingTier 100 `
        -ExercisePriceGate `
        -SkipControlledRestart `
        -AuthorizeCalculatedMinimumQuantity `
        -AllowDemoOrders
    if ($LASTEXITCODE -ne 0) {
        throw "Targeted V2 reliability canary failed."
    }
    Write-Host "JIT PRICE REJECTION: PASS"
    Write-Host "VALID TRADE AFTER REJECTION: PASS"
    Write-Host "NETWORK DEGRADATION: PASS"
    Write-Host "FILL-LEVEL PNL CANARY: PASS"
    Write-Host "RELIABILITY CANARY OVERALL: PASS"
}
finally {
    Pop-Location
}
