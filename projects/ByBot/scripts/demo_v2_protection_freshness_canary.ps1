param(
    [switch]$AllowDemoOrders,
    [string]$RunId = "",
    [ValidateSet("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")]
    [string]$Symbol = "XRPUSDT",
    [decimal]$MaxNotionalUSDT = 125
)

$ErrorActionPreference = "Stop"
if (-not $AllowDemoOrders) {
    throw "Explicit -AllowDemoOrders is required."
}

$root = Split-Path -Parent $PSScriptRoot
$canary = Join-Path $PSScriptRoot "bybit_demo_canary.ps1"
$python = Join-Path $root ".venv\Scripts\python.exe"

& powershell -ExecutionPolicy Bypass -File $canary `
    -RunId $RunId `
    -Symbol $Symbol `
    -MaxNotionalUSDT $MaxNotionalUSDT `
    -V2SizingTier 100 `
    -ExerciseStaleProtectionFreshness `
    -AuthorizeCalculatedMinimumQuantity `
    -AllowDemoOrders
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

# Stage B is deliberately local fault injection. A real unprotected position
# is never created merely to prove the hard-failure branch.
& $python -m pytest -q `
    "tests/test_protection_data_freshness.py::test_initial_protection_deadline_expiry_fails_fast" `
    "tests/test_protection_data_freshness.py::test_true_unprotected_replay_still_fails"
if ($LASTEXITCODE -ne 0) {
    throw "Synthetic unsafe protection stage failed."
}

Write-Host "STAGE A SAFE STALE MANAGEMENT DEFERRAL: PASS"
Write-Host "STAGE B SYNTHETIC UNPROTECTED HARD FAILURE: PASS"
Write-Host "PROTECTION FRESHNESS CANARY: PASS"
