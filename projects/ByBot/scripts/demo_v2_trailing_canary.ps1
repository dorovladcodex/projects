param(
    [switch]$AllowDemoOrders,
    [ValidateSet("BTCUSDT", "ETHUSDT")]
    [string]$Symbol = "BTCUSDT",
    [decimal]$MaxNotionalUSDT = 75
)

$ErrorActionPreference = "Stop"
if (-not $AllowDemoOrders) {
    throw "Explicit -AllowDemoOrders is required."
}

$scriptPath = Join-Path $PSScriptRoot "bybit_demo_canary.ps1"
& powershell -ExecutionPolicy Bypass -File $scriptPath `
    -Symbol $Symbol `
    -MaxNotionalUSDT $MaxNotionalUSDT `
    -StartupTimeoutSeconds 360 `
    -ExerciseTrailingUpdate `
    -AuthorizeCalculatedMinimumQuantity `
    -AllowDemoOrders
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
