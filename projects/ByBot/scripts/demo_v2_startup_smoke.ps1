[CmdletBinding()]
param(
    [switch]$InternalTest,
    [ValidateSet('','Success','Failure','Timeout')]
    [string]$InternalNativeChild = ''
)

$ErrorActionPreference = 'Stop'
if ($InternalNativeChild) {
    if ($InternalNativeChild -eq 'Success') {
        [Console]::Error.WriteLine('stderr-ok')
        exit 0
    }
    if ($InternalNativeChild -eq 'Failure') { exit 7 }
    Start-Sleep -Seconds 3
    exit 0
}
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$runId = 'demo-v2-startup-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$artifactDir = Join-Path $root ('artifacts\demo-v2\' + $runId)
$reportPath = Join-Path $artifactDir 'startup-smoke-report.json'
$process = $null
$savedEnvironment = @{}
$startedAt = [DateTime]::UtcNow
$readinessSeconds = $null
$failure = $null
$port = 8137

function Invoke-NativeCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [int]$TimeoutSeconds = 120,
        [string]$Stage = 'NATIVE COMMAND',
        [switch]$Quiet
    )
    $stdoutFile = [IO.Path]::GetTempFileName()
    $stderrFile = [IO.Path]::GetTempFileName()
    $native = $null
    try {
        $native = Start-Process -FilePath $FilePath -ArgumentList $Arguments `
            -NoNewWindow -PassThru -RedirectStandardOutput $stdoutFile `
            -RedirectStandardError $stderrFile
        [void]$native.Handle
        if (-not $native.WaitForExit($TimeoutSeconds * 1000)) {
            try {
                $killer = Start-Process taskkill.exe `
                    -ArgumentList @('/PID',[string]$native.Id,'/T','/F') `
                    -WindowStyle Hidden -PassThru
                $killer.WaitForExit(); $killer.Dispose()
            } catch {
                if (-not $native.HasExited) { $native.Kill() }
            }
            $native.WaitForExit(); $native.Refresh()
            throw ($Stage + ' timed out after ' + $TimeoutSeconds + ' seconds')
        }
        $native.WaitForExit(); $native.Refresh()
        $code = $null
        try { $code = $native.ExitCode } catch { $code = $null }
        $stdout = [IO.File]::ReadAllText($stdoutFile)
        $stderr = [IO.File]::ReadAllText($stderrFile)
        if (-not $Quiet) {
            if ($stdout) { Write-Host $stdout.TrimEnd() }
            if ($stderr) { Write-Host $stderr.TrimEnd() }
        }
        if ($null -eq $code) {
            throw ('Native command exit code was not captured: stage=' + $Stage)
        }
        if ($code -ne 0) {
            throw ($Stage + ' failed with exit code ' + $code)
        }
        return @{ ExitCode = $code; Stdout = $stdout; Stderr = $stderr }
    } finally {
        if ($null -ne $native) { $native.Dispose() }
        Remove-Item -Force -ErrorAction SilentlyContinue $stdoutFile,$stderrFile
    }
}

function Set-ChildEnvironment([string]$Name, [string]$Value) {
    if (-not $savedEnvironment.ContainsKey($Name)) {
        $savedEnvironment[$Name] = [Environment]::GetEnvironmentVariable(
            $Name, 'Process'
        )
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

function Restore-Environment {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable(
            $name, $savedEnvironment[$name], 'Process'
        )
    }
}

function Get-ComposeDatabaseService {
    $result = Invoke-NativeCommand docker @('compose','config','--services') `
        -TimeoutSeconds 30 -Stage 'COMPOSE SERVICES' -Quiet
    $services = @($result.Stdout -split "`r?`n" | Where-Object { $_ })
    $service = $services | Where-Object { $_ -match '(^db$|postgres)' } |
        Select-Object -First 1
    if (-not $service) { throw 'PostgreSQL compose service was not found.' }
    return $service
}

function Get-HostPostgresPort([string]$Service) {
    $result = Invoke-NativeCommand docker `
        @('compose','port',$Service,'5432') -TimeoutSeconds 30 `
        -Stage 'POSTGRESQL PORT' -Quiet
    if ($result.Stdout.Trim() -notmatch ':(\d+)\s*$') {
        throw 'PostgreSQL mapped port was not reported.'
    }
    return [int]$Matches[1]
}

function Set-HostDatabaseEnvironment([int]$MappedPort) {
    $databaseUrl = [Environment]::GetEnvironmentVariable('DATABASE_URL','Process')
    if (-not $databaseUrl) {
        $line = Get-Content (Join-Path $root '.env') -ErrorAction SilentlyContinue |
            Where-Object { $_ -match '^\s*DATABASE_URL=' } |
            Select-Object -First 1
        if ($line) { $databaseUrl = ($line -split '=',2)[1].Trim() }
    }
    if (-not $databaseUrl) { throw 'DATABASE_URL is not configured.' }
    $hostUrl = $databaseUrl -replace '@(?:db|localhost|127\.0\.0\.1):\d+/',
        ('@127.0.0.1:' + $MappedPort + '/')
    if ($hostUrl -notmatch ('@127\.0\.0\.1:' + $MappedPort + '/')) {
        throw 'DATABASE_URL could not be normalized for host Python.'
    }
    Set-ChildEnvironment 'DATABASE_URL' $hostUrl
}

function Get-PortOwners {
    try {
        return @(Get-NetTCPConnection -LocalPort $port -State Listen `
            -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        $owners = @()
        foreach ($line in @(netstat.exe -ano -p tcp 2>$null)) {
            if ($line -match ('^\s*TCP\s+\S+:' + $port +
                '\s+\S+\s+LISTENING\s+(\d+)\s*$')) {
                $owners += [int]$Matches[1]
            }
        }
        return @($owners | Select-Object -Unique)
    }
}

function Stop-Uvicorn {
    if ($null -eq $script:process) { return }
    $pidValue = $script:process.Id
    try {
        if (-not $script:process.HasExited) {
            $killer = Start-Process taskkill.exe `
                -ArgumentList @('/PID',[string]$pidValue,'/T','/F') `
                -WindowStyle Hidden -PassThru
            $killer.WaitForExit(); $killer.Dispose()
        }
    } catch {
        Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
    }
    try { $script:process.WaitForExit() } catch { }
    try { $script:process.Dispose() } catch { }
    $script:process = $null
    Start-Sleep -Milliseconds 500
}

function Save-StartupFailureDiagnostics {
    if ($null -eq $script:process) { return }
    try {
        $all = @(Get-CimInstance Win32_Process -ErrorAction Stop)
        $ids = New-Object System.Collections.Generic.List[int]
        $pending = New-Object System.Collections.Generic.Queue[int]
        $pending.Enqueue([int]$script:process.Id)
        while ($pending.Count -gt 0) {
            $current = $pending.Dequeue()
            if ($ids.Contains($current)) { continue }
            $ids.Add($current)
            foreach ($child in @($all | Where-Object {
                [int]$_.ParentProcessId -eq $current
            })) {
                $pending.Enqueue([int]$child.ProcessId)
            }
        }
        @($all | Where-Object {
            $ids.Contains([int]$_.ProcessId)
        } | Select-Object ProcessId,ParentProcessId,Name,CreationDate) |
            ConvertTo-Json -Depth 5 |
            Set-Content (Join-Path $artifactDir 'startup-process-tree.json') `
                -Encoding UTF8
    } catch {
        Write-Warning ('Process-tree diagnostic failed: ' +
            $_.Exception.GetType().Name)
    }
    # Python's startup watchdog writes thread and asyncio task stacks directly
    # under artifactDir\startup. Redirected Uvicorn logs already reside beside
    # this process-tree snapshot and are read only after process cleanup.
}

function Write-SmokeReport(
    [string]$Result,
    [object]$Health,
    [object]$V2Status,
    [object]$StartupStatus
) {
    $finishedAt = [DateTime]::UtcNow
    $orders = $null
    if ($null -ne $V2Status -and
        $null -ne $V2Status.signal_metrics) {
        $orders = $V2Status.signal_metrics.orders_submitted
    }
    $payload = [ordered]@{
        run_id = $runId
        result = $Result
        started_at = $startedAt.ToString('o')
        finished_at = $finishedAt.ToString('o')
        duration_seconds = ($finishedAt - $startedAt).TotalSeconds
        readiness_seconds = $readinessSeconds
        health = $Health
        persistence_status = if ($null -ne $V2Status) {
            $V2Status.persistence_status
        } else { $null }
        startup = $StartupStatus
        orders_submitted = $orders
        exchange_mutations_performed = $false
        uvicorn_pid = if ($null -ne $script:process) {
            $script:process.Id
        } else { $null }
        failure = $failure
    }
    New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null
    $payload | ConvertTo-Json -Depth 20 |
        Set-Content -LiteralPath $reportPath -Encoding UTF8
}

function Invoke-InternalTests {
    $ok = Invoke-NativeCommand powershell.exe `
        @('-NoProfile','-ExecutionPolicy','Bypass','-File',$PSCommandPath,
          '-InternalNativeChild','Success') `
        -TimeoutSeconds 10 -Stage 'INTERNAL SUCCESS' -Quiet
    if ($ok.ExitCode -ne 0 -or $ok.Stderr -notmatch 'stderr-ok') {
        throw 'Native success/stderr validation failed.'
    }
    $failed = $false
    try {
        Invoke-NativeCommand powershell.exe `
            @('-NoProfile','-ExecutionPolicy','Bypass','-File',$PSCommandPath,
              '-InternalNativeChild','Failure') -TimeoutSeconds 10 `
            -Stage 'INTERNAL FAILURE' -Quiet | Out-Null
    } catch {
        $failed = $_.Exception.Message -match 'exit code 7'
    }
    if (-not $failed) { throw 'Native non-zero validation failed.' }
    $timedOut = $false
    try {
        Invoke-NativeCommand powershell.exe `
            @('-NoProfile','-ExecutionPolicy','Bypass','-File',$PSCommandPath,
              '-InternalNativeChild','Timeout') `
            -TimeoutSeconds 1 -Stage 'INTERNAL TIMEOUT' -Quiet | Out-Null
    } catch {
        $timedOut = $_.Exception.Message -match 'timed out'
    }
    if (-not $timedOut) { throw 'Native timeout validation failed.' }
    Write-Host 'INTERNAL PROCESS HELPERS: PASS'
}

if ($InternalTest) {
    Invoke-InternalTests
    exit 0
}

if (-not (Test-Path $python)) { throw 'Local .venv Python was not found.' }

$health = $null
$v2Status = $null
$startupStatus = $null
try {
    New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null
    if (@(Get-PortOwners).Count -ne 0) {
        throw ('Port ' + $port + ' already has a listener.')
    }
    Invoke-NativeCommand docker @('--version') -Stage 'DOCKER VERSION' | Out-Null
    $dbService = Get-ComposeDatabaseService
    Invoke-NativeCommand docker @('compose','up','-d',$dbService) `
        -Stage 'POSTGRESQL START' | Out-Null
    $healthy = $false
    for ($i = 0; $i -lt 60; $i++) {
        try {
            Invoke-NativeCommand docker `
                @('compose','exec','-T',$dbService,'pg_isready','-U','bybot') `
                -TimeoutSeconds 10 -Stage 'POSTGRESQL HEALTH' -Quiet | Out-Null
            $healthy = $true; break
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $healthy) { throw 'PostgreSQL did not become healthy.' }
    Set-HostDatabaseEnvironment (Get-HostPostgresPort $dbService)
    Invoke-NativeCommand $python @('-m','alembic','upgrade','head') `
        -Stage 'ALEMBIC UPGRADE' | Out-Null
    Invoke-NativeCommand $python @('scripts\demo_kill_switch_diagnostics.py') `
        -Stage 'READ-ONLY DIAGNOSTICS' | Out-Null
    Invoke-NativeCommand $python @('scripts\demo_v2_preflight.py') `
        -Stage 'READ-ONLY PREFLIGHT' | Out-Null

    Set-ChildEnvironment 'APP_ENV' 'local'
    Set-ChildEnvironment 'TEST_MODE' 'false'
    Set-ChildEnvironment 'BOT_MODE' 'PAPER'
    Set-ChildEnvironment 'EXECUTION_MODE' 'PAPER'
    Set-ChildEnvironment 'BYBIT_ENABLE_TRADING' 'false'
    Set-ChildEnvironment 'BYBIT_LIVE_TRADING_ENABLED' 'false'
    Set-ChildEnvironment 'BYBIT_DEMO_TRADING_ENABLED' 'false'
    Set-ChildEnvironment 'DEMO_ORDER_EXECUTION_AUTHORIZED' 'false'
    Set-ChildEnvironment 'V2_ENABLED' 'true'
    Set-ChildEnvironment 'V2_AUTO_DEMO_EXECUTION' 'false'
    Set-ChildEnvironment 'DEMO_RUN_ID' $runId
    Set-ChildEnvironment 'V2_REPORT_DIRECTORY' (
        Join-Path $root 'artifacts\demo-v2'
    )

    $stdoutPath = Join-Path $artifactDir 'uvicorn.stdout.log'
    $stderrPath = Join-Path $artifactDir 'uvicorn.stderr.log'
    $script:process = Start-Process -FilePath $python -ArgumentList @(
        '-m','uvicorn','app.main:app','--host','127.0.0.1',
        '--port',[string]$port
    ) -WorkingDirectory $root -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath -WindowStyle Hidden -PassThru
    $readyStarted = [DateTime]::UtcNow
    $deadline = $readyStarted.AddSeconds(60)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($script:process.HasExited) {
            $script:process.WaitForExit(); $script:process.Refresh()
            throw ('Uvicorn exited before readiness: code=' +
                $script:process.ExitCode)
        }
        try {
            $health = Invoke-RestMethod (
                'http://127.0.0.1:' + $port + '/health'
            ) -TimeoutSec 2
            break
        } catch { Start-Sleep -Milliseconds 250 }
    }
    if ($null -eq $health) {
        throw 'FastAPI readiness exceeded 60 seconds.'
    }
    $readinessSeconds = ([DateTime]::UtcNow - $readyStarted).TotalSeconds
    $v2Status = Invoke-RestMethod (
        'http://127.0.0.1:' + $port + '/v2/status'
    ) -TimeoutSec 3
    $startupStatus = Invoke-RestMethod (
        'http://127.0.0.1:' + $port + '/startup/status'
    ) -TimeoutSec 3
    if ($health.status -ne 'ok') { throw '/health did not report ok.' }
    if ($v2Status.persistence_status -ne 'OK') {
        throw '/v2/status persistence_status is not OK.'
    }
    if ($startupStatus.state -ne 'READY') {
        throw 'Startup diagnostics did not report READY.'
    }
    if ([int]$v2Status.signal_metrics.orders_submitted -ne 0) {
        throw 'Startup unexpectedly submitted a Demo order.'
    }
    Write-SmokeReport 'PASS' $health $v2Status $startupStatus
    Write-Host ('STARTUP READINESS: PASS seconds=' +
        ([decimal]$readinessSeconds).ToString())
    Write-Host 'HEALTH: PASS'
    Write-Host 'PERSISTENCE: PASS'
    Write-Host 'DEMO ORDER MUTATIONS: 0 PASS'
} catch {
    $failure = $_.Exception.Message
    Save-StartupFailureDiagnostics
    try {
        $startupFile = Join-Path $artifactDir `
            'startup\startup-diagnostics.json'
        if (Test-Path $startupFile) {
            $startupStatus = Get-Content $startupFile -Raw |
                ConvertFrom-Json
        }
    } catch { }
    Write-SmokeReport 'FAIL' $health $v2Status $startupStatus
    Write-Error ($failure + '; report=' + $reportPath)
} finally {
    Stop-Uvicorn
    $owners = @(Get-PortOwners)
    if ($owners.Count -ne 0) {
        Write-Warning ('Port cleanup failed; owners=' + ($owners -join ','))
    } else {
        Write-Host 'PROCESS CLEANUP: PASS'
    }
    Restore-Environment
}

Write-Host ('REPORT: ' + $reportPath)
Write-Host 'OVERALL: PASS'
