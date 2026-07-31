[CmdletBinding()]
param(
    [ValidateRange(0.25, 168)][double]$Hours = 24,
    [ValidateSet('DISCOVERY', 'STRICT')][string]$CertificationMode = 'STRICT',
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
$finalValidationRan = $false
$finalDiagnosticsPass = $false
$finalAnalysisPass = $false
$uvicornCleanupPass = $true
$analysisMarkdownPath = Join-Path $artifactDir 'analysis\analysis.md'

function Invoke-NativeCommand {
    param(
        [string]$FilePath, [string[]]$Arguments,
        [int]$TimeoutSeconds = 120,
        [string]$Stage = 'NATIVE COMMAND'
    )
    $stdoutFile = [IO.Path]::GetTempFileName()
    $stderrFile = [IO.Path]::GetTempFileName()
    $p = $null
    try {
        $p = Start-Process -FilePath $FilePath -ArgumentList $Arguments `
            -NoNewWindow -PassThru -RedirectStandardOutput $stdoutFile `
            -RedirectStandardError $stderrFile
        # Force Windows PowerShell 5.1 to retain the native process handle.
        # Without this, Start-Process can return a PID-only proxy whose
        # ExitCode remains null after a fast redirected child exits.
        [void]$p.Handle
        if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
            try {
                $killer = Start-Process -FilePath 'taskkill.exe' `
                    -ArgumentList @('/PID', [string]$p.Id, '/T', '/F') `
                    -NoNewWindow -PassThru
                $killer.WaitForExit()
                $killer.Dispose()
            } catch {
                if (-not $p.HasExited) { $p.Kill() }
            }
            $p.WaitForExit()
            $p.Refresh()
            $stdout = [IO.File]::ReadAllText($stdoutFile)
            $stderr = [IO.File]::ReadAllText($stderrFile)
            if ($stdout) { Write-Host $stdout.TrimEnd() }
            if ($stderr) { Write-Host $stderr.TrimEnd() }
            throw "$Stage timed out after $TimeoutSeconds seconds"
        }

        # Windows PowerShell 5.1 may not populate ExitCode after the timed
        # overload until the final asynchronous stream callbacks complete.
        $p.WaitForExit()
        $p.Refresh()
        $code = $null
        try { $code = $p.ExitCode } catch { $code = $null }
        $stdout = [IO.File]::ReadAllText($stdoutFile)
        $stderr = [IO.File]::ReadAllText($stderrFile)
        if ($stdout) { Write-Host $stdout.TrimEnd() }
        if ($stderr) { Write-Host $stderr.TrimEnd() }
        if ($null -eq $code) {
            throw "Native command exit code was not captured: stage=$Stage"
        }
        if ($code -ne 0) { throw "$Stage failed with exit code $code" }
    } finally {
        if ($null -ne $p) { $p.Dispose() }
        Remove-Item -Force -ErrorAction SilentlyContinue $stdoutFile,$stderrFile
    }
}

function Get-HostPostgresPort {
    param(
        [string]$Service,
        [int]$TimeoutSeconds = 30,
        [string]$DockerExecutable = 'docker',
        [string[]]$DockerPrefixArguments = @()
    )
    $out = [IO.Path]::GetTempFileName(); $err = [IO.Path]::GetTempFileName()
    $p = $null
    try {
        $dockerArguments = @($DockerPrefixArguments) + @('compose','port',$Service,'5432')
        $p = Start-Process -FilePath $DockerExecutable -ArgumentList $dockerArguments `
            -NoNewWindow -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
        [void]$p.Handle
        if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
            try {
                $killer = Start-Process -FilePath 'taskkill.exe' `
                    -ArgumentList @('/PID', [string]$p.Id, '/T', '/F') `
                    -NoNewWindow -PassThru
                $killer.WaitForExit()
                $killer.Dispose()
            } catch {
                if (-not $p.HasExited) { $p.Kill() }
            }
            $p.WaitForExit()
            $p.Refresh()
            $stdout = [IO.File]::ReadAllText($out)
            $stderr = [IO.File]::ReadAllText($err)
            if ($stdout) { Write-Host $stdout.TrimEnd() }
            if ($stderr) { Write-Host $stderr.TrimEnd() }
            throw "POSTGRESQL PORT timed out after $TimeoutSeconds seconds"
        }

        $p.WaitForExit()
        $p.Refresh()
        $code = $null
        try { $code = $p.ExitCode } catch { $code = $null }
        $stdout = [IO.File]::ReadAllText($out)
        $stderr = [IO.File]::ReadAllText($err)
        if ($stderr) { Write-Host $stderr.TrimEnd() }
        if ($null -eq $code) {
            throw 'Native command exit code was not captured: stage=POSTGRESQL PORT'
        }
        if ($code -ne 0) {
            if ($stdout) { Write-Host $stdout.TrimEnd() }
            throw "POSTGRESQL PORT failed with exit code $code"
        }
        $text = $stdout.Trim()
        if ($text -notmatch ':(\d+)\s*$') { throw 'PostgreSQL mapped port was not reported.' }
        return [int]$Matches[1]
    } finally {
        if ($null -ne $p) { $p.Dispose() }
        Remove-Item -Force -ErrorAction SilentlyContinue $out,$err
    }
}

function Set-HostDatabaseEnvironment([int]$Port) {
    $db = [Environment]::GetEnvironmentVariable('DATABASE_URL', 'Process')
    if (-not $db) {
        $line = Get-Content (Join-Path $root '.env') -ErrorAction SilentlyContinue |
            Where-Object { $_ -match '^\s*DATABASE_URL=' } | Select-Object -First 1
        if ($line) { $db = ($line -split '=',2)[1].Trim() }
    }
    if (-not $db) { throw 'DATABASE_URL is not configured.' }
    $hostDb = $db -replace '@(?:db|localhost|127\.0\.0\.1):\d+/', ("@127.0.0.1:$Port/")
    if ($hostDb -notmatch '@127\.0\.0\.1:' + $Port + '/') {
        throw 'DATABASE_URL host could not be safely normalized for host Python.'
    }
    Set-ChildEnvironment 'DATABASE_URL' $hostDb
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
        $rootPid = $script:process.Id
        try {
            $treeIds = @(Get-ProcessTreeIds -RootProcessId $rootPid)
        } catch {
            $treeIds = @($rootPid)
        }
        try {
            if (-not $script:process.HasExited) {
                $graceful = Start-Process -FilePath 'taskkill.exe' `
                    -ArgumentList @('/PID', [string]$rootPid, '/T') `
                    -PassThru -WindowStyle Hidden
                $graceful.WaitForExit()
                $graceful.Dispose()
                [void]$script:process.WaitForExit(5000)
            }
        } catch {
            Write-Warning ('Graceful Uvicorn stop did not complete: ' + $_.Exception.Message)
        }
        $remaining = @(
            $treeIds | Where-Object {
                Get-Process -Id $_ -ErrorAction SilentlyContinue
            }
        )
        if ($remaining.Count -gt 0) {
            try {
                $forced = Start-Process -FilePath 'taskkill.exe' `
                    -ArgumentList @('/PID', [string]$rootPid, '/T', '/F') `
                    -PassThru -WindowStyle Hidden
                $forced.WaitForExit()
                $forced.Dispose()
            } catch {
                foreach ($processId in $remaining) {
                    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
                }
            }
        }
        try { $script:process.WaitForExit() } catch { }
        try { $script:process.Dispose() } catch { }
        $script:process = $null
        Start-Sleep -Milliseconds 500
        $survivors = @(
            $treeIds | Where-Object {
                Get-Process -Id $_ -ErrorAction SilentlyContinue
            }
        )
        if ($survivors.Count -gt 0) {
            Write-Warning ('Uvicorn process-tree cleanup left PIDs: ' + ($survivors -join ','))
        }
    }
}

function Get-ProcessTreeIds {
    param([int]$RootProcessId)
    $result = New-Object System.Collections.Generic.List[int]
    $pending = New-Object System.Collections.Generic.Queue[int]
    $pending.Enqueue($RootProcessId)
    while ($pending.Count -gt 0) {
        $current = $pending.Dequeue()
        if ($result.Contains($current)) { continue }
        $result.Add($current)
        $children = @(Get-CimInstance Win32_Process -Filter (
            'ParentProcessId=' + $current
        ) -ErrorAction SilentlyContinue)
        foreach ($child in $children) { $pending.Enqueue([int]$child.ProcessId) }
    }
    return @($result)
}

function Test-ProcessInTree {
    param([int]$ProcessId, [int]$RootProcessId)
    $current = $ProcessId
    $seen = @{}
    while ($current -gt 0 -and -not $seen.ContainsKey($current)) {
        if ($current -eq $RootProcessId) { return $true }
        $seen[$current] = $true
        $row = Get-CimInstance Win32_Process -Filter (
            'ProcessId=' + $current
        ) -ErrorAction SilentlyContinue
        if ($null -eq $row) { return $false }
        $current = [int]$row.ParentProcessId
    }
    return $false
}

function Get-UvicornPortOwners {
    $owners = @()
    try {
        $owners = @(Get-NetTCPConnection -LocalPort 8137 -State Listen `
            -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        $lines = @(netstat.exe -ano -p tcp 2>$null)
        foreach ($line in $lines) {
            if ($line -match '^\s*TCP\s+\S+:8137\s+\S+\s+LISTENING\s+(\d+)\s*$') {
                $owners += [int]$Matches[1]
            }
        }
        $owners = @($owners | Select-Object -Unique)
    }
    return @($owners)
}

function Assert-SingleUvicornOwner {
    param([int]$ExpectedPid)
    $hasExpectedPid = $PSBoundParameters.ContainsKey('ExpectedPid')
    $owners = @(Get-UvicornPortOwners)
    if (-not $hasExpectedPid) {
        if ($owners.Count -ne 0) {
            throw ('Port 8137 already has a listener; refusing duplicate Uvicorn: pid=' + ($owners -join ','))
        }
        return
    }
    $ownedByTree = $false
    if ($owners.Count -eq 1) {
        $ownedByTree = Test-ProcessInTree `
            -ProcessId ([int]$owners[0]) -RootProcessId $ExpectedPid
    }
    if ($owners.Count -ne 1 -or -not $ownedByTree) {
        throw ('Port 8137 ownership mismatch: expected-tree-root=' + $ExpectedPid + '; actual=' + ($owners -join ','))
    }
}

function Wait-UvicornReady {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$BaseUrl,
        [int]$TimeoutSeconds = 240
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process.HasExited) {
            $Process.WaitForExit()
            $Process.Refresh()
            $startupPath = Join-Path $script:artifactDir 'startup\startup-diagnostics.json'
            if (Test-Path -LiteralPath $startupPath) {
                try {
                    $startup = Get-Content -Raw -LiteralPath $startupPath |
                        ConvertFrom-Json
                    if ($startup.persistence_connect.state -eq 'FAIL') {
                        throw (
                            'BOUNDED PERSISTENCE STARTUP FAILURE: attempts=' +
                            $startup.persistence_connect.attempt_count +
                            '; category=' +
                            $startup.persistence_connect.last_error_category +
                            '; exit_code=' + $Process.ExitCode
                        )
                    }
                } catch {
                    if ($_.Exception.Message -like 'BOUNDED PERSISTENCE*') {
                        throw
                    }
                }
            }
            throw ('UVICORN CRASH BEFORE READINESS: exit_code=' + $Process.ExitCode)
        }
        try {
            $null = Invoke-RestMethod ($BaseUrl + '/health') -TimeoutSec 2
            Assert-SingleUvicornOwner -ExpectedPid $Process.Id
            return
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw (
        'APPLICATION READINESS TIMEOUT: FastAPI did not become ready within ' +
        $TimeoutSeconds + ' seconds.'
    )
}

function Assert-StartupPersistenceReady {
    param([string]$BaseUrl)
    $startup = Invoke-RestMethod ($BaseUrl + '/startup/status') -TimeoutSec 10
    if ($startup.state -ne 'READY' -or
        $startup.startup_final_result -ne 'PASS' -or
        $startup.persistence_connect.state -ne 'PASS' -or
        $startup.persistence_connect.repository_available -ne $true -or
        $startup.persistence_connect.health_query_passed -ne $true) {
        throw 'STARTUP STATUS VALIDATION failed: persistence is not authoritatively ready.'
    }
    Write-Host (
        'PERSISTENCE READINESS: PASS attempts=' +
        $startup.persistence_connect.attempt_count +
        ' elapsed_ms=' + $startup.persistence_connect.elapsed_ms
    )
}

function Assert-AuthoritativeOrderOwnership {
    param([string]$BaseUrl)
    $demo = Invoke-RestMethod ($BaseUrl + '/demo/status') -TimeoutSec 3
    $confirmed = [int]$demo.confirmed_unrelated_orders
    $conflicts = [int]$demo.ownership_conflicts
    if ($confirmed -eq 0 -and $conflicts -eq 0) { return }

    # Never fail from a cached mixed-age snapshot.  The production
    # reconciliation path performs exact-ID ownership and bounded exact
    # protection cancellation before publishing one atomic status snapshot.
    $null = Invoke-RestMethod ($BaseUrl + '/demo/reconcile') `
        -Method Post -TimeoutSec 15
    $demo = Invoke-RestMethod ($BaseUrl + '/demo/status') -TimeoutSec 3
    if ([int]$demo.ownership_conflicts -gt 0) {
        throw 'Authoritative Demo ownership conflict detected.'
    }
    if ([int]$demo.confirmed_unrelated_orders -gt 0) {
        throw 'Authoritative unrelated Demo order detected.'
    }
}

function Invoke-FinalValidation {
    if ($script:finalValidationRan) { return }
    $script:finalValidationRan = $true
    Stop-Uvicorn
    try { Assert-SingleUvicornOwner } catch {
        Write-Warning ('Uvicorn cleanup ownership check failed: ' + $_.Exception.Message)
        $script:uvicornCleanupPass = $false
    }
    try {
        Invoke-NativeCommand -FilePath $script:python `
            -Arguments @((Join-Path $script:root 'scripts\demo_kill_switch_diagnostics.py')) `
            -TimeoutSeconds 120 -Stage 'FINAL READ-ONLY DIAGNOSTICS'
        $script:finalDiagnosticsPass = $script:uvicornCleanupPass
    } catch {
        Write-Warning ('Final read-only diagnostics failed: ' + $_.Exception.Message)
    }
    try {
        Invoke-NativeCommand -FilePath $script:python `
            -Arguments @((Join-Path $script:root 'scripts\analyze_demo_v2_run.py'), '--run-id', $script:runId) `
            -TimeoutSeconds 180 -Stage 'FINAL RUN ANALYSIS'
        $script:finalAnalysisPass = $true
    } catch {
        Write-Warning ('Final run analysis failed: ' + $_.Exception.Message)
    }
    Write-Host ('ANALYSIS: ' + $script:analysisMarkdownPath)
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
    Set-ChildEnvironment 'V2_CERTIFICATION_MODE' $CertificationMode
    Set-ChildEnvironment 'DEMO_CANARY_ENABLED' 'false'
    Set-ChildEnvironment 'DEMO_RUN_ID' $runId
    Set-ChildEnvironment 'DEMO_RUN_STARTED_AT' ([DateTime]::UtcNow.ToString('o'))
    Set-ChildEnvironment 'ALLOWED_SYMBOLS' '["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","LINKUSDT","AVAXUSDT","SUIUSDT","NEARUSDT","LTCUSDT","TONUSDT","PEPEUSDT","SHIBUSDT","WIFUSDT","BONKUSDT","FLOKIUSDT"]'
    Set-ChildEnvironment 'V2_REPORT_DIRECTORY' (Join-Path $root 'artifacts\demo-v2')
    Push-Location $root
    try {
        Invoke-NativeCommand -FilePath 'docker' -Arguments @('--version') -Stage 'DOCKER VERSION'
        $databaseService = Get-ComposeDatabaseService
        Invoke-NativeCommand -FilePath 'docker' `
            -Arguments @('compose', 'up', '-d', $databaseService) `
            -Stage 'POSTGRESQL START'
        $dbReady = $false
        for ($i=0; $i -lt 60; $i++) {
            if (Test-PostgresReady $databaseService) { $dbReady = $true; break }
            Start-Sleep -Seconds 1
        }
        if (-not $dbReady) { throw 'PostgreSQL did not become healthy.' }
        Write-Host 'POSTGRESQL: PASS'
        Set-HostDatabaseEnvironment (Get-HostPostgresPort $databaseService)
        Write-Host 'ALEMBIC: START'
        Invoke-NativeCommand -FilePath $python `
            -Arguments @('-m', 'alembic', 'upgrade', 'head') `
            -TimeoutSeconds 120 -Stage 'ALEMBIC UPGRADE'
        Write-Host 'ALEMBIC: PASS'
        Write-Host 'READ-ONLY PREFLIGHT: START'
        Invoke-NativeCommand -FilePath $python `
            -Arguments @('scripts\demo_v2_preflight.py') `
            -TimeoutSeconds 120 -Stage 'READ-ONLY PREFLIGHT'
        Write-Host 'READ-ONLY PREFLIGHT: PASS'

        $deadline = [DateTime]::UtcNow.AddHours($Hours)
        Set-ChildEnvironment 'V2_RUN_NOMINAL_END_AT' $deadline.ToString('o')
        Write-Host ('CERTIFICATION MODE: ' + $CertificationMode)
        Write-Host 'UVICORN: START'
        Assert-SingleUvicornOwner
        $stdout = Join-Path $artifactDir 'uvicorn.stdout.log'
        $stderr = Join-Path $artifactDir 'uvicorn.stderr.log'
        $script:process = Start-Process -FilePath $python -ArgumentList @(
            '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8137'
        ) -WorkingDirectory $root -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -WindowStyle Hidden

        $base = 'http://127.0.0.1:8137'
        Wait-UvicornReady -Process $script:process -BaseUrl $base
        Assert-StartupPersistenceReady -BaseUrl $base
        $status = Invoke-RestMethod "$base/v2/status" -TimeoutSec 5
        if (-not $status.preflight_ok) { throw ('V2 runtime preflight failed: ' + ($status.preflight_blockers -join '; ')) }
        Write-Host ('V2 STARTED: PASS run_id=' + $runId)

        while ([DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Seconds 30
            $restartReason = $null
            if ($script:process.HasExited) {
                $restartReason = 'process exited'
            } else {
                try {
                    # HasExited is insufficient on Windows: the process can
                    # survive an accept-loop socket failure while port 8137 is
                    # no longer listening.  Port ownership is the authoritative
                    # readiness invariant for this local runner.
                    Assert-SingleUvicornOwner -ExpectedPid $script:process.Id
                } catch {
                    $restartReason = 'listener ownership lost'
                }
            }
            if ($null -ne $restartReason) {
                Write-Warning (
                    'FastAPI unavailable (' + $restartReason +
                    '); restarting with the same durable run_id.'
                )
                Stop-Uvicorn
                $script:process = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8137') -WorkingDirectory $root -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -WindowStyle Hidden
                Wait-UvicornReady -Process $script:process -BaseUrl $base
            }
            try {
                $status = Invoke-RestMethod "$base/v2/status" -TimeoutSec 3
                Assert-AuthoritativeOrderOwnership -BaseUrl $base
                Write-Host ("{0:u} symbols={1} signals={2} open={3} kill={4}" -f [DateTime]::UtcNow,$status.accepted_symbols.Count,$status.signal_count,$status.open_reservations,$status.kill_switch_active)
                if ($status.kill_switch_active) { Write-Warning 'Kill switch active; no reset will be attempted.' }
            } catch { Write-Warning 'Transient status failure; durable reconciliation remains authoritative.' }
        }

        $null = Invoke-RestMethod "$base/v2/stop-new-entries" -Method Post -TimeoutSec 5
        $drainComplete = $false
        $drainTimedOut = $false
        $drainBlockers = @()
        $drainTimeoutIncidentWritten = $false
        while (-not $drainComplete) {
            if ($script:process.HasExited) {
                throw 'FastAPI exited during bounded drain reconciliation.'
            }
            try {
                $status = Invoke-RestMethod "$base/v2/status" -TimeoutSec 3
                Assert-AuthoritativeOrderOwnership -BaseUrl $base
                Write-Host (
                    "{0:u} phase={1} active={2} drain_remaining={3}" -f `
                    [DateTime]::UtcNow,$status.run_phase,`
                    $status.drain_active_execution_ids.Count,`
                    $status.drain_seconds_remaining
                )
                $drainComplete = [string]$status.run_phase -eq 'FINISHED'
                if ([bool]$status.drain_timed_out) {
                    $drainTimedOut = $true
                }
                $drainBlockers = @($status.drain_safety_blockers)
            } catch {
                Write-Warning 'Transient drain status failure; retrying while Uvicorn is healthy.'
            }
            if ($drainTimedOut -and -not $drainTimeoutIncidentWritten) {
                $incident = [ordered]@{
                    run_id = $runId
                    detected_at = [DateTime]::UtcNow.ToString('o')
                    event_type = 'V2_DRAIN_TIMEOUT'
                    phase = [string]$status.run_phase
                    active_execution_ids = @($status.drain_active_execution_ids)
                    safety_blockers = @($status.drain_safety_blockers)
                    position_management_continues = $true
                }
                $incident | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 `
                    (Join-Path $artifactDir 'drain-timeout-incident.json')
                $drainTimeoutIncidentWritten = $true
                Write-Warning (
                    'Bounded drain timed out; incident preserved and position ' +
                    'management remains active until durable terminalization.'
                )
            }
            if (-not $drainComplete) {
                Start-Sleep -Seconds 10
            }
        }
        if ($OptionalForceDemoCleanup) {
            $cleanup = Invoke-RestMethod "$base/demo/cleanup" -Method Post -TimeoutSec 60
            if (-not $cleanup.live_execution_blocked) { throw 'Demo cleanup safety assertion failed.' }
        }
        $report = Invoke-RestMethod "$base/v2/report" -Method Post -TimeoutSec 30
        Invoke-FinalValidation
        $cycleFailuresZero = (
            [int]$status.safety_critical_failures -eq 0 -and
            [int]$status.data_integrity_failures -eq 0 -and
            [int]$status.unexpected_cycle_failures -eq 0
        )
        $runFinished = [string]$status.run_phase -eq 'FINISHED'
        $safetyResult = $(if (
            -not $drainTimedOut -and $script:finalDiagnosticsPass -and `
            $script:finalAnalysisPass -and $cycleFailuresZero -and $runFinished
        ) { 'PASS' } else { 'FAIL' })
        $report | Add-Member -NotePropertyName safety_result `
            -NotePropertyValue $safetyResult -Force
        $report | Add-Member -NotePropertyName final_read_only_diagnostics `
            -NotePropertyValue $(if ($script:finalDiagnosticsPass) { 'PASS' } else { 'FAIL' }) -Force
        $report | Add-Member -NotePropertyName final_run_analysis `
            -NotePropertyValue $(if ($script:finalAnalysisPass) { 'PASS' } else { 'FAIL' }) -Force
        $report | Add-Member -NotePropertyName final_cycle_failures_zero `
            -NotePropertyValue $cycleFailuresZero -Force
        $report | Add-Member -NotePropertyName final_run_phase_finished `
            -NotePropertyValue $runFinished -Force
        $report | Add-Member -NotePropertyName drain_completed `
            -NotePropertyValue $drainComplete -Force
        $report | Add-Member -NotePropertyName drain_timed_out `
            -NotePropertyValue $drainTimedOut -Force
        $report | Add-Member -NotePropertyName drain_safety_blockers `
            -NotePropertyValue $drainBlockers -Force
        $report | ConvertTo-Json -Depth 20 | Set-Content -Encoding UTF8 (Join-Path $artifactDir 'runner-report.json')
        $functionalResult = [string]$report.functional_result
        if (-not $functionalResult) { $functionalResult = 'FAIL' }
        Write-Host ('FUNCTIONAL RESULT: ' + $functionalResult)
        Write-Host ('SAFETY RESULT: ' + $safetyResult)
        Write-Host ('ARTIFACTS: ' + $artifactDir)
        if ($functionalResult -notin @('PASS', 'PASS_WITH_WARNINGS')) {
            throw ('V2 functional validation failed: ' + ($report.functional_blockers -join '; '))
        }
        if ($safetyResult -ne 'PASS') {
            throw 'V2 safety validation failed: unresolved durable execution or remote-state disagreement'
        }
    } finally { Pop-Location }
} finally {
    Stop-Uvicorn
    try { Invoke-FinalValidation } catch {
        Write-Warning ('Final validation cleanup warning: ' + $_.Exception.Message)
    }
    foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
    if ($hasMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
