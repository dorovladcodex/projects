[CmdletBinding()]
param(
    [ValidateRange(0.25, 168)][double]$Hours = 24,
    [switch]$AllowDemoOrders,
    [switch]$OptionalGracefulShutdown,
    [switch]$OptionalForceDemoCleanup
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$runId = 'demo-v2-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$artifactDir = Join-Path $root ("artifacts\demo-v2\" + $runId)
$mutex = New-Object System.Threading.Mutex($false, 'Global\ByBotDemoV2Soak')
$hasMutex = $false
$process = $null
$saved = @{}

function Invoke-NativeCommand {
    param([string]$FilePath, [string[]]$Arguments)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath; $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    # ProcessStartInfo.ArgumentList is unavailable on Windows PowerShell 5.1.
    $psi.Arguments = ($Arguments | ForEach-Object {
        '"' + ([string]$_).Replace('"','\"') + '"'
    }) -join ' '
    $p = New-Object System.Diagnostics.Process; $p.StartInfo = $psi
    [void]$p.Start(); $stdout = $p.StandardOutput.ReadToEnd(); $stderr = $p.StandardError.ReadToEnd()
    $p.WaitForExit(); $code = $p.ExitCode; $p.Dispose()
    if ($stdout) { Write-Host $stdout.TrimEnd() }
    if ($stderr) { Write-Host $stderr.TrimEnd() }
    if ($code -ne 0) { throw "Native command failed with exit code $code" }
}

function Get-ComposeDatabaseService {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'docker'; $psi.Arguments = '"compose" "config" "--services"'
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    $p = New-Object System.Diagnostics.Process; $p.StartInfo = $psi; [void]$p.Start()
    $stdout = $p.StandardOutput.ReadToEnd(); $stderr = $p.StandardError.ReadToEnd()
    $p.WaitForExit(); $code = $p.ExitCode; $p.Dispose()
    if ($code -ne 0) { throw "docker compose config failed with exit code $code" }
    $services = @($stdout -split "`r?`n" | Where-Object { $_ })
    $match = $services | Where-Object { $_ -match '(^db$|postgres)' } | Select-Object -First 1
    if (-not $match) { throw 'PostgreSQL compose service could not be detected.' }
    return $match
}

function Test-PostgresReady([string]$Service) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'docker'
    $psi.Arguments = '"compose" "exec" "-T" "' + $Service + '" "pg_isready" "-U" "bybot"'
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    $p = New-Object System.Diagnostics.Process; $p.StartInfo = $psi; [void]$p.Start()
    $p.WaitForExit(); $ok = $p.ExitCode -eq 0; $p.Dispose(); return $ok
}

function Set-ChildEnvironment([string]$Name, [string]$Value) {
    if (-not $saved.ContainsKey($Name)) { $saved[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process') }
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

function Stop-Uvicorn {
    if ($null -ne $script:process) {
        if (-not $script:process.HasExited) { $script:process.Kill(); $script:process.WaitForExit() }
        $script:process.Dispose(); $script:process = $null
    }
}

if (-not $AllowDemoOrders) { throw 'Explicit -AllowDemoOrders is required.' }
if (-not (Test-Path $python)) { throw 'Local .venv Python was not found.' }

try {
    $hasMutex = $mutex.WaitOne(0)
    if (-not $hasMutex) { throw 'Another ByBot V2 soak instance is running.' }
    New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null

    Set-ChildEnvironment 'APP_ENV' 'demo'
    Set-ChildEnvironment 'TEST_MODE' 'false'
    Set-ChildEnvironment 'BOT_MODE' 'BYBIT_DEMO'
    Set-ChildEnvironment 'EXECUTION_MODE' 'BYBIT_DEMO'
    Set-ChildEnvironment 'BYBIT_ENV' 'demo'
    Set-ChildEnvironment 'BYBIT_ENABLE_TRADING' 'false'
    Set-ChildEnvironment 'BYBIT_LIVE_TRADING_ENABLED' 'false'
    Set-ChildEnvironment 'BYBIT_DEMO_TRADING_ENABLED' 'true'
    Set-ChildEnvironment 'DEMO_ORDER_EXECUTION_AUTHORIZED' 'true'
    Set-ChildEnvironment 'V2_ENABLED' 'true'
    Set-ChildEnvironment 'V2_AUTO_DEMO_EXECUTION' 'true'
    Set-ChildEnvironment 'DEMO_CANARY_ENABLED' 'false'
    Set-ChildEnvironment 'DEMO_RUN_ID' $runId
    Set-ChildEnvironment 'DEMO_RUN_STARTED_AT' ([DateTime]::UtcNow.ToString('o'))
    Set-ChildEnvironment 'ALLOWED_SYMBOLS' '["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","LINKUSDT","AVAXUSDT","SUIUSDT","NEARUSDT","LTCUSDT","TONUSDT","PEPEUSDT","SHIBUSDT","WIFUSDT","BONKUSDT","FLOKIUSDT"]'
    Set-ChildEnvironment 'V2_REPORT_DIRECTORY' (Join-Path $root 'artifacts\demo-v2')
    $db = [Environment]::GetEnvironmentVariable('DATABASE_URL', 'Process')
    if ($db) { Set-ChildEnvironment 'DATABASE_URL' ($db -replace '@db:', '@127.0.0.1:') }

    Push-Location $root
    try {
        Invoke-NativeCommand 'docker' @('--version')
        $databaseService = Get-ComposeDatabaseService
        Invoke-NativeCommand 'docker' @('compose', 'up', '-d', $databaseService)
        $dbReady = $false
        for ($i=0; $i -lt 60; $i++) {
            if (Test-PostgresReady $databaseService) { $dbReady = $true; break }
            Start-Sleep -Seconds 1
        }
        if (-not $dbReady) { throw 'PostgreSQL did not become healthy.' }
        Write-Host 'POSTGRESQL: PASS'
        Invoke-NativeCommand $python @('-m', 'alembic', 'upgrade', 'head')
        Write-Host 'ALEMBIC: PASS'
        Invoke-NativeCommand $python @('scripts\demo_v2_preflight.py')
        Write-Host 'READ-ONLY PREFLIGHT: PASS'

        $stdout = Join-Path $artifactDir 'uvicorn.stdout.log'
        $stderr = Join-Path $artifactDir 'uvicorn.stderr.log'
        $script:process = Start-Process -FilePath $python -ArgumentList @(
            '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8137'
        ) -WorkingDirectory $root -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -WindowStyle Hidden

        $base = 'http://127.0.0.1:8137'
        $ready = $false
        for ($i=0; $i -lt 90; $i++) {
            if ($script:process.HasExited) { throw "FastAPI exited before readiness: $($script:process.ExitCode)" }
            try { $null = Invoke-RestMethod "$base/health" -TimeoutSec 2; $ready = $true; break } catch { Start-Sleep -Seconds 1 }
        }
        if (-not $ready) { throw 'FastAPI did not become ready.' }
        $status = Invoke-RestMethod "$base/v2/status" -TimeoutSec 5
        if (-not $status.preflight_ok) { throw ('V2 runtime preflight failed: ' + ($status.preflight_blockers -join '; ')) }
        Write-Host ('V2 STARTED: PASS run_id=' + $runId)

        $deadline = [DateTime]::UtcNow.AddHours($Hours)
        while ([DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Seconds 30
            if ($script:process.HasExited) {
                Write-Warning 'FastAPI stopped; restarting with the same durable run_id.'
                Stop-Uvicorn
                $script:process = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8137') -WorkingDirectory $root -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -WindowStyle Hidden
                Start-Sleep -Seconds 5
            }
            try {
                $status = Invoke-RestMethod "$base/v2/status" -TimeoutSec 3
                Write-Host ("{0:u} symbols={1} signals={2} open={3} kill={4}" -f [DateTime]::UtcNow,$status.accepted_symbols.Count,$status.signal_count,$status.open_reservations,$status.kill_switch_active)
                if ($status.kill_switch_active) { Write-Warning 'Kill switch active; no reset will be attempted.' }
            } catch { Write-Warning 'Transient status failure; durable reconciliation remains authoritative.' }
        }

        $null = Invoke-RestMethod "$base/v2/stop-new-entries" -Method Post -TimeoutSec 5
        if ($OptionalGracefulShutdown) { Start-Sleep -Seconds 300 }
        if ($OptionalForceDemoCleanup) {
            $cleanup = Invoke-RestMethod "$base/demo/cleanup" -Method Post -TimeoutSec 60
            if (-not $cleanup.live_execution_blocked) { throw 'Demo cleanup safety assertion failed.' }
        }
        $report = Invoke-RestMethod "$base/v2/report" -Method Post -TimeoutSec 30
        $report | ConvertTo-Json -Depth 20 | Set-Content -Encoding UTF8 (Join-Path $artifactDir 'runner-report.json')
        Write-Host ('FUNCTIONAL RESULT: PASS')
        Write-Host ('SAFETY RESULT: ' + $(if ($status.kill_switch_active) { 'FAIL_CLOSED' } else { 'PASS' }))
        Write-Host ('ARTIFACTS: ' + $artifactDir)
    } finally { Pop-Location }
} finally {
    Stop-Uvicorn
    foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
    if ($hasMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
