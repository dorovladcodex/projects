param(
    [double]$Hours = 12,
    [int]$SampleSeconds = 30,
    [switch]$AllowDemoOrders,
    [int]$MaxTemporaryOutageSamples = 20
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$script:Child = $null
$script:OriginalEnvironment = @{}
$script:OverallPassed = $false

function Assert-Condition {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Test-LeverageIsOne {
    param($Value)
    if ($null -eq $Value) { return $false }
    [decimal]$parsed = 0
    $ok = [decimal]::TryParse(
        [string]$Value,
        [Globalization.NumberStyles]::Number,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$parsed
    )
    return $ok -and $parsed -eq [decimal]1
}

function Protect-Text {
    param([string]$Text)
    if (-not $Text) { return $Text }
    $safe = $Text
    foreach ($name in @("DATABASE_URL", "BYBIT_API_KEY", "BYBIT_API_SECRET", "LLM_API_KEY")) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if ($value) { $safe = $safe -replace [regex]::Escape($value), "[REDACTED]" }
    }
    return $safe
}

function Invoke-NativeCommand {
    param([string]$FilePath, [string[]]$Arguments, [string]$Label)
    $out = [IO.Path]::GetTempFileName()
    $err = [IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments `
            -NoNewWindow -Wait -PassThru -RedirectStandardOutput $out `
            -RedirectStandardError $err
        $exitCode = $process.ExitCode
        $stdout = [IO.File]::ReadAllText($out)
        $stderr = [IO.File]::ReadAllText($err)
        if ($stdout) { Write-Host (Protect-Text $stdout.TrimEnd()) }
        if ($stderr) { Write-Host (Protect-Text $stderr.TrimEnd()) }
        if ($exitCode -ne 0) {
            throw "$Label failed with exit code $exitCode"
        }
        return $stdout
    }
    finally {
        Remove-Item -LiteralPath $out, $err -Force -ErrorAction SilentlyContinue
    }
}

function Get-EnvFileValue {
    param([string]$Name)
    if (-not (Test-Path -LiteralPath ".env")) { return $null }
    foreach ($line in [IO.File]::ReadAllLines((Resolve-Path ".env"))) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*)\s*$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

function Set-IsolatedEnvironment {
    param([string]$Name, [string]$Value)
    if (-not $script:OriginalEnvironment.ContainsKey($Name)) {
        $script:OriginalEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

function Restore-Environment {
    foreach ($name in $script:OriginalEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $script:OriginalEnvironment[$name], "Process")
    }
}

function Get-AvailablePort {
    $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $port
}

function Stop-Uvicorn {
    if ($null -eq $script:Child) { return }
    try {
        if (-not $script:Child.HasExited) {
            $script:Child.CloseMainWindow() | Out-Null
            if (-not $script:Child.WaitForExit(5000)) {
                $script:Child.Kill()
                $script:Child.WaitForExit()
            }
        }
    }
    finally {
        $script:Child.Dispose()
        $script:Child = $null
        Start-Sleep -Milliseconds 300
    }
}

function Safe-ReadTextFile {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return "" }
    try {
        $stream = New-Object IO.FileStream(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite
        )
        try {
            $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8, $true)
            try { return $reader.ReadToEnd() }
            finally { $reader.Dispose() }
        }
        finally { $stream.Dispose() }
    }
    catch {
        return ""
    }
}

function Get-SanitizedStderrTail {
    param([string]$Path, [int]$LineCount = 25)
    $text = Safe-ReadTextFile -Path $Path
    if (-not $text) { return "(no stderr output captured)" }
    $lines = @($text -split "`r?`n" | Where-Object { $_.Trim() })
    if ($lines.Count -gt $LineCount) {
        $lines = @($lines[($lines.Count - $LineCount)..($lines.Count - 1)])
    }
    return Protect-Text ($lines -join [Environment]::NewLine)
}

function Get-SanitizedStartupErrorSummary {
    param([string]$Path)
    $text = Safe-ReadTextFile -Path $Path
    if (-not $text) {
        return @{
            FirstExceptionLine = "(not available)"
            FinalErrorLine = "(not available)"
        }
    }
    $lines = @($text -split "`r?`n" | ForEach-Object { $_.Trim() } |
        Where-Object { $_ })
    $errorLines = @($lines | Where-Object {
        $_ -match '(?i)(traceback|exception|error|failed|failure)'
    })
    $first = if ($errorLines.Count -gt 0) { $errorLines[0] } else { $lines[0] }
    $last = if ($errorLines.Count -gt 0) {
        $errorLines[$errorLines.Count - 1]
    }
    else {
        $lines[$lines.Count - 1]
    }
    return @{
        FirstExceptionLine = Protect-Text $first
        FinalErrorLine = Protect-Text $last
    }
}

function Get-UvicornExitCode {
    if ($null -eq $script:Child) { return $null }
    try {
        if (-not $script:Child.HasExited) { return $null }
        $script:Child.WaitForExit()
        $exitCode = [int]$script:Child.ExitCode
        # A clean process code before either readiness endpoint responded is not
        # a meaningful startup result (Start-Process/Windows can surface 0 for
        # a launcher that exited before the child startup failure was observed).
        if ($exitCode -eq 0) { return $null }
        return $exitCode
    }
    catch {
        return $null
    }
}

function New-UvicornStartupFailure {
    param([string]$Reason, $ExitCode)
    $exitText = if ($null -eq $ExitCode) { "unknown" } else { [string]$ExitCode }
    $reportText = if ($script:ReportPath) { $script:ReportPath } else { "not created" }
    $stderrTail = Get-SanitizedStderrTail -Path $script:StderrPath
    $errorSummary = Get-SanitizedStartupErrorSummary -Path $script:StderrPath
    return @(
        $Reason,
        "FastAPI exit code: $exitText",
        "First exception line: $($errorSummary.FirstExceptionLine)",
        "Final error line: $($errorSummary.FinalErrorLine)",
        "Last relevant sanitized stderr lines:",
        $stderrTail,
        "Report path: $reportText"
    ) -join [Environment]::NewLine
}

function Start-Uvicorn {
    param([string]$Python, [int]$Port, [string]$Stdout, [string]$Stderr)
    $script:Child = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
        "--port", "$Port", "--workers", "1", "--no-access-log"
    ) -PassThru -WindowStyle Hidden -RedirectStandardOutput $Stdout `
      -RedirectStandardError $Stderr
}

function Invoke-Api {
    param([string]$Method = "GET", [string]$Path, [int]$TimeoutSec = 15)
    $uri = "$script:BaseUrl$Path"
    try {
        return Invoke-RestMethod -Method $Method -Uri $uri -TimeoutSec $TimeoutSec
    }
    catch {
        $status = $null
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        throw "API $Method $Path failed (HTTP $status): $($_.Exception.Message)"
    }
}

function Wait-ForApi {
    param([int]$TimeoutSeconds = 90)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($script:Child -and $script:Child.HasExited) {
            $exitCode = Get-UvicornExitCode
            throw (New-UvicornStartupFailure `
                -Reason "FastAPI exited during startup" -ExitCode $exitCode)
        }
        try {
            $health = Invoke-Api -Path "/health" -TimeoutSec 3
            if ($health.status -eq "ok") { return }
        }
        catch {
            try {
                $status = Invoke-Api -Path "/status" -TimeoutSec 3
                if ($null -ne $status) { return }
            }
            catch { }
        }
        Start-Sleep -Seconds 1
    }
    throw (New-UvicornStartupFailure `
        -Reason "FastAPI did not become healthy before the startup timeout" `
        -ExitCode $null)
}

function Assert-DemoPreflight {
    $demo = Invoke-Api -Path "/demo/status"
    Assert-Condition ($demo.enabled -eq $true) "Demo execution is not enabled"
    Assert-Condition ($demo.environment -eq "demo") "Execution environment is not Demo"
    Assert-Condition ($demo.rest_domain -eq "https://api-demo.bybit.com") "Unexpected Demo REST domain"
    Assert-Condition ($demo.private_ws_domain -eq "wss://stream-demo.bybit.com") "Unexpected private WebSocket domain"
    Assert-Condition ($demo.account_verified -eq $true) "Demo account is not verified"
    Assert-Condition ($demo.leverage -eq 1) "Demo leverage is not exactly 1"
    Assert-Condition ($null -ne $demo.PSObject.Properties["symbol_leverage"]) `
        "Demo per-symbol leverage status is missing"
    Assert-Condition ($null -ne $demo.symbol_leverage.PSObject.Properties["BTCUSDT"]) `
        "BTCUSDT leverage status is missing"
    Assert-Condition ($null -ne $demo.symbol_leverage.PSObject.Properties["ETHUSDT"]) `
        "ETHUSDT leverage status is missing"
    Assert-Condition (Test-LeverageIsOne $demo.symbol_leverage.BTCUSDT.buy) `
        "BTCUSDT buy leverage is not exactly 1"
    Assert-Condition (Test-LeverageIsOne $demo.symbol_leverage.BTCUSDT.sell) `
        "BTCUSDT sell leverage is not exactly 1"
    Assert-Condition (Test-LeverageIsOne $demo.symbol_leverage.ETHUSDT.buy) `
        "ETHUSDT buy leverage is not exactly 1"
    Assert-Condition (Test-LeverageIsOne $demo.symbol_leverage.ETHUSDT.sell) `
        "ETHUSDT sell leverage is not exactly 1"
    Assert-Condition ($demo.leverage_normalized -eq $true) `
        "Demo leverage normalization was not confirmed"
    Write-Host "DEMO LEVERAGE BTCUSDT: 1x PASS"
    Write-Host "DEMO LEVERAGE ETHUSDT: 1x PASS"
    Write-Host "DEMO LEVERAGE NORMALIZATION: PASS"
    $reconciliation = Invoke-Api -Method "POST" -Path "/demo/reconcile"
    Assert-Condition ($reconciliation.status -eq "OK") `
        "Demo USDT reconciliation did not pass"
    Assert-Condition ($null -ne $reconciliation.PSObject.Properties["open_orders_by_symbol"]) `
        "Demo per-symbol open-order reconciliation status is missing"
    foreach ($symbol in @("BTCUSDT", "ETHUSDT")) {
        Assert-Condition ($null -ne $reconciliation.open_orders_by_symbol.PSObject.Properties[$symbol]) `
            "$symbol open-order reconciliation status is missing"
        $count = [int]$reconciliation.open_orders_by_symbol.$symbol
        Write-Host "DEMO OPEN ORDERS ${symbol}: $count"
    }
    Assert-Condition ($reconciliation.usdt_order_reconciliation -eq "PASS") `
        "Demo USDT order reconciliation did not pass"
    Assert-Condition ($reconciliation.usdt_position_reconciliation -eq "PASS") `
        "Demo USDT position reconciliation did not pass"
    Write-Host "DEMO USDT ORDER RECONCILIATION: PASS"
    Write-Host "DEMO USDT POSITION RECONCILIATION: PASS"
    $status = Invoke-Api -Path "/status"
    Assert-Condition ($status.execution_mode -eq "BYBIT_DEMO") "Wrong execution mode"
    Assert-Condition ($status.live_order_placement_blocked -eq $true) "Live execution is not blocked"
    Assert-Condition ($status.bybit_live_trading_enabled -eq $false) "Live trading flag is enabled"
    Assert-Condition ($status.active_symbols.Count -eq 2) "Active symbol count changed"
    Assert-Condition ($status.active_symbols -contains "BTCUSDT") "BTCUSDT is missing"
    Assert-Condition ($status.active_symbols -contains "ETHUSDT") "ETHUSDT is missing"
    $restore = Invoke-Api -Path "/news/restore-status"
    Assert-Condition ($restore.restore_completed -eq $true) "News startup restore did not complete"
    Assert-Condition ($restore.persistence_status -eq "OK") "News persistence restore is unavailable"
    Assert-Condition ($null -ne $restore.PSObject.Properties["news_restore_quarantined_count"]) `
        "News restore quarantine metrics are missing"
    $script:RestoreStatus = $restore
    if ([int]$restore.news_restore_quarantined_count -gt 0) {
        Write-Warning ("Historical news rows quarantined: " + $restore.news_restore_quarantined_count)
    }
    return $demo
}

function Assert-NoInvalidCurrentRunCandidates {
    param([DateTime]$RunStartedAt)
    $restore = Invoke-Api -Path "/news/restore-status"
    $quarantined = @($restore.quarantined_news_ids)
    if ($quarantined.Count -eq 0) { return }
    $candidateResponse = Invoke-Api -Path "/signals/candidates"
    foreach ($candidate in @($candidateResponse.candidates)) {
        $createdAt = [DateTimeOffset]::Parse([string]$candidate.created_at).UtcDateTime
        if ($createdAt -ge $RunStartedAt -and $quarantined -contains [string]$candidate.news_id) {
            throw "Current-run Demo candidate references quarantined news item"
        }
    }
}

if (-not $AllowDemoOrders) {
    Write-Error "-AllowDemoOrders is required as explicit human confirmation."
    exit 1
}
if ($Hours -le 0 -or $SampleSeconds -lt 5) {
    Write-Error "Hours must be positive and SampleSeconds must be at least 5."
    exit 1
}

try {
    Set-Location (Split-Path -Parent $PSScriptRoot)
    $python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
    Assert-Condition (Test-Path -LiteralPath $python) "Local .venv Python is missing"
    Invoke-NativeCommand "docker" @("version") "Docker CLI" | Out-Null
    Invoke-NativeCommand "docker" @("compose", "version") "Docker Compose" | Out-Null

    $databaseUrl = [Environment]::GetEnvironmentVariable("DATABASE_URL", "Process")
    if (-not $databaseUrl) { $databaseUrl = Get-EnvFileValue "DATABASE_URL" }
    Assert-Condition ([bool]$databaseUrl) "DATABASE_URL is not configured"
    $databaseUrl = $databaseUrl -replace "@db:", "@127.0.0.1:"
    $databaseUrl = $databaseUrl -replace "@localhost:", "@127.0.0.1:"

    $runId = "demo-" + [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $runStartedAt = [DateTime]::UtcNow
    $artifactDir = Join-Path (Get-Location) "artifacts\demo-soak\$runId"
    New-Item -ItemType Directory -Path $artifactDir -Force | Out-Null
    $snapshotsPath = Join-Path $artifactDir "snapshots.jsonl"
    $reportPath = Join-Path $artifactDir "report.md"
    $summaryPath = Join-Path $artifactDir "summary.json"
    $stdoutPath = Join-Path $artifactDir "uvicorn.stdout.log"
    $stderrPath = Join-Path $artifactDir "uvicorn.stderr.log"
    $script:ReportPath = $reportPath
    $script:StderrPath = $stderrPath

    Set-IsolatedEnvironment "APP_ENV" "demo"
    Set-IsolatedEnvironment "TEST_MODE" "false"
    Set-IsolatedEnvironment "BOT_MODE" "BYBIT_DEMO"
    Set-IsolatedEnvironment "EXECUTION_MODE" "BYBIT_DEMO"
    Set-IsolatedEnvironment "BYBIT_ENV" "demo"
    Set-IsolatedEnvironment "BYBIT_DEMO_TRADING_ENABLED" "true"
    Set-IsolatedEnvironment "DEMO_ORDER_EXECUTION_AUTHORIZED" "true"
    Set-IsolatedEnvironment "BYBIT_LIVE_TRADING_ENABLED" "false"
    Set-IsolatedEnvironment "BYBIT_ENABLE_TRADING" "false"
    Set-IsolatedEnvironment "AUTO_PAPER_EXECUTION" "false"
    Set-IsolatedEnvironment "BYBIT_PRIVATE_DEMO_BASE_URL" "https://api-demo.bybit.com"
    Set-IsolatedEnvironment "BYBIT_PRIVATE_DEMO_WS_URL" "wss://stream-demo.bybit.com"
    Set-IsolatedEnvironment "DEMO_RISK_CAPITAL_USDT" "10000"
    Set-IsolatedEnvironment "DEMO_LEVERAGE" "1"
    Set-IsolatedEnvironment "DEMO_RUN_ID" $runId
    Set-IsolatedEnvironment "DEMO_RUN_STARTED_AT" $runStartedAt.ToString("o")
    Set-IsolatedEnvironment "ACTIVE_SYMBOLS" '["BTCUSDT","ETHUSDT"]'
    Set-IsolatedEnvironment "MARKET_DATA_PROVIDER" "BYBIT_REST"
    Set-IsolatedEnvironment "NEWS_ENABLE_RSS" "true"
    Set-IsolatedEnvironment "DATABASE_URL" $databaseUrl

    Invoke-NativeCommand "docker" @("compose", "up", "-d", "db") "PostgreSQL start" | Out-Null
    $healthy = $false
    for ($i = 0; $i -lt 60; $i++) {
        $container = (docker compose ps -q db 2>$null)
        if ($LASTEXITCODE -eq 0 -and $container) {
            $health = (docker inspect --format '{{.State.Health.Status}}' $container 2>$null)
            if ($LASTEXITCODE -eq 0 -and $health -eq "healthy") { $healthy = $true; break }
        }
        Start-Sleep -Seconds 2
    }
    Assert-Condition $healthy "PostgreSQL did not become healthy"
    Invoke-NativeCommand $python @("-m", "alembic", "upgrade", "head") "Alembic" | Out-Null

    $port = Get-AvailablePort
    $script:BaseUrl = "http://127.0.0.1:$port"
    Start-Uvicorn $python $port $stdoutPath $stderrPath
    Wait-ForApi
    $opening = Assert-DemoPreflight
    Write-Host "DEMO PREFLIGHT: PASS"
    Write-Host "DEMO ACCOUNT VERIFIED: PASS"
    Write-Host "LIVE EXECUTION BLOCKED: PASS"

    $runBoundary = Invoke-Api -Path "/demo/run-report"
    Assert-Condition ($runBoundary.run_id -eq $runId) "Durable Demo run ID does not match"
    $startedAt = [DateTimeOffset]::Parse([string]$runBoundary.started_at).UtcDateTime
    $finishAt = $startedAt.AddHours($Hours)
    $restartAt = $startedAt.AddTicks([long](($finishAt - $startedAt).Ticks / 2))
    $restarted = $false
    $outageSamples = 0
    Write-Host "DEMO SOAK RUNNING: $Hours hour(s), sampling every $SampleSeconds seconds"

    while ([DateTime]::UtcNow -lt $finishAt) {
        if (-not $restarted -and [DateTime]::UtcNow -ge $restartAt) {
            Stop-Uvicorn
            Start-Uvicorn $python $port $stdoutPath $stderrPath
            Wait-ForApi
            $null = Assert-DemoPreflight
            $null = Invoke-Api -Method "POST" -Path "/demo/reconcile"
            Assert-NoInvalidCurrentRunCandidates -RunStartedAt $startedAt
            $restarted = $true
            Write-Host "CONTROLLED RESTART: PASS"
        }
        try {
            $status = Invoke-Api -Path "/status"
            $demo = Invoke-Api -Path "/demo/status"
            Assert-NoInvalidCurrentRunCandidates -RunStartedAt $startedAt
            Assert-Condition ($status.live_order_placement_blocked -eq $true) "Live execution safety changed"
            Assert-Condition ($demo.rest_domain -eq "https://api-demo.bybit.com") "Demo REST domain changed"
            Assert-Condition (-not $demo.kill_switch_active) ("Demo kill switch active: " + ($demo.kill_switch_reasons -join "; "))
            $snapshot = [ordered]@{
                sampled_at = [DateTime]::UtcNow.ToString("o")
                run_id = $runId
                market_data_status = $status.market_data_status
                news_status = $status.news_status
                classifier_status = $status.news_classifier_status
                demo = $demo
                candidates = @{
                    total = $status.signal_candidates_count
                    pending = $status.pending_signal_candidates_count
                    ready = $status.ready_signal_candidates_count
                }
            }
            Add-Content -LiteralPath $snapshotsPath -Value ($snapshot | ConvertTo-Json -Depth 12 -Compress) -Encoding UTF8
            if ($status.market_data_status -ne "OK" -or $status.news_status -eq "ERROR") {
                $outageSamples++
            }
            Assert-Condition ($outageSamples -le $MaxTemporaryOutageSamples) "Temporary outage threshold exceeded"
        }
        catch { throw }
        $remaining = ($finishAt - [DateTime]::UtcNow).TotalSeconds
        if ($remaining -gt 0) { Start-Sleep -Seconds ([Math]::Min($SampleSeconds, [int][Math]::Ceiling($remaining))) }
    }

    if (-not $restarted) {
        Stop-Uvicorn
        Start-Uvicorn $python $port $stdoutPath $stderrPath
        Wait-ForApi
        $null = Assert-DemoPreflight
        $restarted = $true
        Write-Host "CONTROLLED RESTART: PASS"
    }

    $cleanup = Invoke-Api -Method "POST" -Path "/demo/cleanup"
    $flat = $false
    for ($i = 0; $i -lt 40; $i++) {
        $reconcile = Invoke-Api -Method "POST" -Path "/demo/reconcile"
        $finalDemo = Invoke-Api -Path "/demo/status"
        if ($finalDemo.bot_owned_open_orders -eq 0 -and $finalDemo.bot_owned_open_positions -eq 0) {
            $flat = $true; break
        }
        Start-Sleep -Seconds 3
    }
    Assert-Condition $flat "Final bot-owned Demo state is not flat"
    Write-Host "EXCHANGE RECONCILIATION: PASS"
    Write-Host "BOT-OWNED CLEANUP: PASS"
    Write-Host "FINAL DEMO STATE FLAT: PASS"

    $status = Invoke-Api -Path "/status"
    $runReport = Invoke-Api -Path "/demo/run-report?finish=true"
    $runActivity = $runReport.activity_this_run
    Assert-Condition (
        [int]$runActivity.candidates_created_this_run -eq
        [int](($runActivity.candidate_final_states_this_run.PSObject.Properties | Measure-Object -Property Value -Sum).Sum)
    ) "Current-run candidate state totals are inconsistent"
    foreach ($candidate in @($runReport.current_run_candidates)) {
        Assert-Condition (-not ([string]$candidate.final_state).StartsWith("PAPER_")) `
            "PAPER candidate state leaked into Demo run report"
    }
    $executions = Invoke-Api -Path "/demo/executions"
    $runExecutions = @($executions.executions | Where-Object { $_.run_id -eq $runId })
    $submitted = $runExecutions.Count
    $accepted = @($runExecutions | Where-Object { $_.order_id }).Count
    $rejected = @($runExecutions | Where-Object { $_.state -eq "DEMO_FAILED" }).Count
    $partial = @($runExecutions | Where-Object {
        $_.accepted_quantity -gt 0 -and $_.accepted_quantity -lt $_.requested_quantity
    }).Count
    $complete = @($runExecutions | Where-Object { $_.accepted_quantity -ge $_.requested_quantity }).Count
    $opened = @($runExecutions | Where-Object { $_.protection_confirmed -eq $true }).Count
    $closed = @($runExecutions | Where-Object { $_.state -eq "DEMO_CLOSED" }).Count
    [decimal]$exchangeFees = 0
    [decimal]$exchangePnl = 0
    [decimal]$shadowPnl = 0
    foreach ($execution in $runExecutions) {
        if ($null -ne $execution.exchange_fees) { $exchangeFees += [decimal]$execution.exchange_fees }
        if ($null -ne $execution.realized_exchange_pnl) { $exchangePnl += [decimal]$execution.realized_exchange_pnl }
        if ($null -ne $execution.paper_shadow_pnl) { $shadowPnl += [decimal]$execution.paper_shadow_pnl }
    }
    $tpCloses = @($runExecutions | Where-Object { $_.close_reason -eq "take_profit" }).Count
    $slCloses = @($runExecutions | Where-Object { $_.close_reason -eq "stop_loss" }).Count
    $timedCloses = @($runExecutions | Where-Object { $_.close_reason -eq "maximum_holding_time" }).Count
    $stateParts = @()
    foreach ($group in ($runExecutions | Group-Object state)) {
        $stateParts += "$($group.Name)=$($group.Count)"
    }
    $candidateStateSummary = $stateParts -join ", "
    $finishedAt = [DateTime]::UtcNow
    [IO.File]::WriteAllText(
        $summaryPath,
        ($runReport | ConvertTo-Json -Depth 20),
        (New-Object Text.UTF8Encoding($false))
    )
    $candidateDetailLines = @()
    foreach ($candidate in @($runReport.current_run_candidates)) {
        $candidateDetailLines += (
            "- $($candidate.candidate_id) | $($candidate.created_at) | " +
            "$($candidate.news_title) | $($candidate.symbol) | " +
            "$($candidate.final_state) / $($candidate.final_action) | " +
            "edge=$($candidate.expected_edge_bps) | " +
            "risk=$($candidate.risk_result.approved) | " +
            "execution=$($candidate.execution_result) | " +
            "reason=$($candidate.block_or_expiry_reason)"
        )
    }
    if ($candidateDetailLines.Count -eq 0) {
        $candidateDetailLines = @("- No Demo candidates were created during this run.")
    }
    $lines = @(
        "# ByBot Bybit Demo soak report", "",
        "## Run scope", "",
        "- Run ID: $runId",
        "- Started: $($startedAt.ToString('o'))",
        "- Finished: $($finishedAt.ToString('o'))",
        "- Duration hours: $([Math]::Round(($finishedAt-$startedAt).TotalHours, 4))",
        "- Environment: BYBIT_DEMO (credentials omitted)",
        "- REST domain: https://api-demo.bybit.com",
        "- Private WebSocket: wss://stream-demo.bybit.com",
        "- Account verified: $($finalDemo.account_verified)",
        "", "## Activity during this run", "",
        "- News seen / accepted: $($runActivity.news_seen_this_run) / $($runActivity.news_accepted_this_run)",
        "- Classifications / trade eligible: $($runActivity.classifications_this_run) / $($runActivity.trade_eligible_classifications_this_run)",
        "- Demo candidates created: $($runActivity.candidates_created_this_run)",
        "- Orders submitted / accepted / rejected: $($runActivity.orders_submitted_this_run) / $($runActivity.orders_accepted_this_run) / $($runActivity.orders_rejected_this_run)",
        "- Fills: $($runActivity.fills_this_run)",
        "- Positions opened / closed: $($runActivity.positions_opened_this_run) / $($runActivity.positions_closed_this_run)",
        "- Exchange fees: $($runActivity.exchange_fees_this_run)",
        "- Exchange realized PnL: $($runActivity.realized_pnl_this_run)",
        "- Partial / complete fills: $partial / $complete",
        "- TP / SL / timed closes: $tpCloses / $slCloses / $timedCloses",
        "- Paper-shadow PnL: $shadowPnl",
        "- Entry/exit slippage: persisted per execution in /demo/executions",
        "- Reconciliation incidents: $($finalDemo.reconciliation_incidents)",
        "- Private WebSocket reconnects: $($finalDemo.websocket_reconnects)",
        "- Classifier mode: $($status.news_classifier_mode)",
        "- Codex CLI calls / cache hits / tokens today: $($status.codex_cli_calls_count) / $($status.codex_cli_cache_hits) / $($status.codex_cli_total_tokens_today)",
        "", "## Opening state", "",
        "- Preexisting candidates: $($runReport.opening_state.preexisting_candidates)",
        "- Cumulative Demo executions: $($runReport.opening_state.cumulative_demo_executions)",
        "", "## Final cumulative state", "",
        "- Cumulative candidates: $($runReport.final_cumulative_state.cumulative_candidates)",
        "- Cumulative Demo executions: $($runReport.final_cumulative_state.cumulative_demo_executions)",
        "- Historical news restored valid / repaired / quarantined: $($script:RestoreStatus.news_restore_valid_count) / $($script:RestoreStatus.news_restore_repaired_count) / $($script:RestoreStatus.news_restore_quarantined_count)",
        "- Run execution states: $candidateStateSummary",
        "- Persisted Demo executions: $($executions.executions.Count)",
        "- Final bot-owned open orders / positions: $($finalDemo.bot_owned_open_orders) / $($finalDemo.bot_owned_open_positions)",
        "- Kill switch active: $($finalDemo.kill_switch_active)",
        "- Kill-switch activations: $($finalDemo.kill_switch_activations)",
        "- Controlled restart: $restarted",
        "- Live execution blocked: $($status.live_order_placement_blocked)",
        "", "## Current-run candidates", ""
    )
    $lines += $candidateDetailLines
    [IO.File]::WriteAllLines($reportPath, $lines, (New-Object Text.UTF8Encoding($false)))
    Write-Host "REPORT: $reportPath"
    Write-Host "OVERALL: PASS"
    $script:OverallPassed = $true
}
catch {
    $failureMessage = Protect-Text $_.Exception.Message
    if ($reportPath) {
        try {
            [IO.File]::WriteAllLines(
                $reportPath,
                @("# ByBot Bybit Demo soak report", "", "- Result: FAIL", "- Failure: $failureMessage"),
                (New-Object Text.UTF8Encoding($false))
            )
            Write-Host "REPORT: $reportPath"
        }
        catch { Write-Warning "Failure report could not be written" }
    }
    [Console]::Error.WriteLine($failureMessage)
}
finally {
    Stop-Uvicorn
    Restore-Environment
}

if (-not $script:OverallPassed) { exit 1 }
exit 0
