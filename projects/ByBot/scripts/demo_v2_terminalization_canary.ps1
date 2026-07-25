param(
    [switch]$AllowDemoOrders,
    [ValidateSet("BTCUSDT", "ETHUSDT", "XRPUSDT")]
    [string]$Symbol = "XRPUSDT",
    [decimal]$MaxNotionalUSDT = 105
)

$ErrorActionPreference = "Stop"
if (-not $AllowDemoOrders) {
    throw "Explicit -AllowDemoOrders is required."
}

$scriptPath = Join-Path $PSScriptRoot "bybit_demo_canary.ps1"
& powershell -ExecutionPolicy Bypass -File $scriptPath `
    -Symbol $Symbol `
    -MaxNotionalUSDT $MaxNotionalUSDT `
    -V2SizingTier 100 `
    -ExerciseFlatDuringProtectionRace `
    -EnterDrainBeforeFlatRace `
    -AuthorizeCalculatedMinimumQuantity `
    -AllowDemoOrders
exit $LASTEXITCODE
