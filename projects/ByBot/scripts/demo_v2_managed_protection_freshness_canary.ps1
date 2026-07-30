param(
    [switch]$AllowDemoOrders,
    [ValidateSet("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")]
    [string]$Symbol = "XRPUSDT",
    [decimal]$MaxNotionalUSDT = 125,
    [ValidateRange(120, 1200)]
    [int]$HardTimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
if (-not $AllowDemoOrders) {
    throw "Explicit -AllowDemoOrders is required."
}

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Local .venv Python is missing."
}

$runId = "demo-canary-" + [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
$artifactDir = Join-Path $root "artifacts\demo-canary\$runId"
$controlDir = Join-Path $artifactDir "managed-control"
$metadataPath = Join-Path $controlDir "managed-canary.json"
$heartbeatPath = Join-Path $controlDir "heartbeat.json"
New-Item -ItemType Directory -Path $controlDir -Force | Out-Null

$launcherOut = Join-Path $controlDir "launcher.stdout.log"
$launcherErr = Join-Path $controlDir "launcher.stderr.log"
$controller = Start-Process -FilePath $python -ArgumentList @(
    "scripts/demo_v2_managed_canary_controller.py",
    "--run-id", $runId,
    "--artifact-dir", $artifactDir,
    "--hard-timeout-seconds", "$HardTimeoutSeconds",
    "--symbol", $Symbol,
    "--max-notional-usdt",
    $MaxNotionalUSDT.ToString([Globalization.CultureInfo]::InvariantCulture)
) -WorkingDirectory $root -RedirectStandardOutput $launcherOut `
  -RedirectStandardError $launcherErr -PassThru -WindowStyle Hidden
[void]$controller.Handle

$deadline = [DateTime]::UtcNow.AddSeconds(15)
while ([DateTime]::UtcNow -lt $deadline) {
    if ((Test-Path -LiteralPath $metadataPath) -and
        (Test-Path -LiteralPath $heartbeatPath)) {
        try {
            $metadata = Get-Content -LiteralPath $metadataPath -Raw |
                ConvertFrom-Json
            # A venv python.exe may be a short-lived launcher shim. Trust the
            # controller's self-recorded PID, not the Start-Process shim PID.
            if ([int]$metadata.controller_pid -gt 0 -and
                [int]$metadata.runner_pid -gt 0) {
                $metadata | ConvertTo-Json -Depth 8
                exit 0
            }
        }
        catch {
            # Atomic metadata replacement can briefly race this read.
        }
    }
    if ($controller.HasExited -and -not (Test-Path -LiteralPath $metadataPath)) {
        $controller.WaitForExit()
        $controller.Refresh()
        throw "Managed canary controller exited before heartbeat; exit=$($controller.ExitCode)"
    }
    Start-Sleep -Milliseconds 250
}

throw "Managed canary heartbeat was not created within 15 seconds."
