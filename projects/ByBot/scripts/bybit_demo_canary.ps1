param(
    [switch]$AllowDemoOrders,
    [ValidateSet("BTCUSDT", "ETHUSDT")]
    [string]$Symbol = "BTCUSDT",
    [Nullable[decimal]]$MaxNotionalUSDT = $null,
    [decimal]$MarketPriceBufferPct = 5,
    [switch]$ExerciseTrailingUpdate,
    [switch]$AuthorizeCalculatedMinimumQuantity,
    [ValidateRange(90, 900)]
    [int]$StartupTimeoutSeconds = 360
)

# This script is intentionally the only human-triggered real Bybit Demo canary.
# It never enables live trading and it uses the application's guarded production
# DemoExecutionService endpoints rather than calling Bybit directly.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$script:Child = $null
$script:OriginalEnvironment = @{}
$script:OverallPassed = $false
$script:FunctionalResult = "FAIL"
$script:SafetyCleanupResult = "NOT_REQUIRED"
$script:ExecutionId = $null
$script:ArtifactDir = $null
$script:StartedAt = [DateTimeOffset]::UtcNow.ToString("o")
$script:FailureStage = "startup"
$script:NoCandidateCreated = $true
$script:NoReservationCreated = $true
$script:NoOrderSubmitted = $true
$script:ReportWritten = $false

function Assert-Condition {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
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

function Invoke-NativeCommand {
    param([string]$FilePath, [string[]]$Arguments, [string]$Label)
    $stdoutPath = [IO.Path]::GetTempFileName()
    $stderrPath = [IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments `
            -NoNewWindow -Wait -PassThru -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        $exitCode = $process.ExitCode
        $stdout = [IO.File]::ReadAllText($stdoutPath)
        $stderr = [IO.File]::ReadAllText($stderrPath)
        if ($stdout) { Write-Host (Protect-Text $stdout.TrimEnd()) }
        # Native progress output on stderr is not a PowerShell error when exit=0.
        if ($stderr) { Write-Host (Protect-Text $stderr.TrimEnd()) }
        if ($exitCode -ne 0) { throw "$Label failed with exit code $exitCode" }
        return $stdout
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-AvailablePort {
    $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $port
}

function Safe-ReadTextFile {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return "" }
    try {
        $stream = New-Object IO.FileStream(
            $Path, [IO.FileMode]::Open, [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite
        )
        try {
            $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8, $true)
            try { return $reader.ReadToEnd() } finally { $reader.Dispose() }
        }
        finally { $stream.Dispose() }
    }
    catch { return "" }
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
        else { $script:Child.WaitForExit() }
    }
    finally {
        $script:Child.Dispose()
        $script:Child = $null
        Start-Sleep -Milliseconds 300
    }
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
    param(
        [string]$Method = "GET", [string]$Path, $Body = $null,
        [int]$TimeoutSec = 20
    )
    $parameters = @{
        Method = $Method
        Uri = "$script:BaseUrl$Path"
        TimeoutSec = $TimeoutSec
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json"
        $parameters.Body = ($Body | ConvertTo-Json -Depth 10 -Compress)
    }
    try { return Invoke-RestMethod @parameters }
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
            $script:Child.WaitForExit()
            throw "FastAPI exited before readiness with exit code $($script:Child.ExitCode)"
        }
        try {
            $health = Invoke-Api -Path "/health" -TimeoutSec 3
            if ($health.status -eq "ok") { return }
        }
        catch { }
        Start-Sleep -Seconds 1
    }
    throw "FastAPI did not become ready"
}

function Get-RunExecutions {
    $response = Invoke-Api -Path "/demo/executions"
    return @($response.executions | Where-Object { $_.run_id -eq $script:RunId })
}

function Get-Execution {
    param([string]$ExecutionId)
    $response = Invoke-Api -Path "/demo/canary/$ExecutionId" -TimeoutSec 3
    return $response.execution
}

function Get-ExecutionStatus {
    param([string]$ExecutionId)
    return Invoke-Api -Path "/demo/canary/$ExecutionId" -TimeoutSec 3
}

function Write-CanaryReport {
    param($Status, [string]$FailureReason = $null, $DemoStatus = $null)
    if (-not $script:ArtifactDir) { return }
    $report = [ordered]@{
        run_id = $script:RunId
        started_at = $script:StartedAt
        execution_id = $script:ExecutionId
        functional_result = $script:FunctionalResult
        safety_cleanup_result = $script:SafetyCleanupResult
        failure_stage = $script:FailureStage
        failure_reason = $FailureReason
        kill_switch_active = if ($DemoStatus) { [bool]$DemoStatus.kill_switch_active } else { $null }
        kill_switch_reasons = if ($DemoStatus) { @($DemoStatus.kill_switch_reasons) } else { @() }
        no_candidate_created = $script:NoCandidateCreated
        no_reservation_created = $script:NoReservationCreated
        no_order_submitted = $script:NoOrderSubmitted
        generated_at = [DateTimeOffset]::UtcNow.ToString("o")
        execution_report = $Status
    }
    $path = Join-Path $script:ArtifactDir "report.json"
    [IO.File]::WriteAllText(
        $path, ($report | ConvertTo-Json -Depth 30),
        (New-Object Text.UTF8Encoding($false))
    )
    $script:ReportWritten = $true
    Write-Host "CANARY REPORT: $path"
}

function Wait-ForExecutionState {
    param([string]$ExecutionId, [string[]]$States, [int]$TimeoutSeconds = 120)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $execution = $null
        try {
            $execution = Get-Execution -ExecutionId $ExecutionId
        }
        catch {
            if ($script:Child -and $script:Child.HasExited) {
                throw "FastAPI exited while polling Demo execution"
            }
            try { $null = Invoke-Api -Path "/health" -TimeoutSec 2 } catch { }
            Start-Sleep -Seconds 2
            continue
        }
        if ($null -ne $execution -and $States -contains [string]$execution.state) {
            return $execution
        }
        if ($null -ne $execution -and $execution.state -in @(
            "DEMO_FAILED", "DEMO_RECONCILIATION_REQUIRED"
        )) {
            throw "Demo execution entered non-recoverable state $($execution.state)"
        }
        Start-Sleep -Seconds 2
    }
    throw "Timed out waiting for Demo execution state: $($States -join ', ')"
}

function Assert-DemoSafety {
    $status = Invoke-Api -Path "/status"
    $demo = Invoke-Api -Path "/demo/status"
    Assert-Condition ($status.live_order_placement_blocked -eq $true) `
        "Live execution is not blocked"
    Assert-Condition ($status.bybit_live_trading_enabled -eq $false) `
        "Live trading flag is enabled"
    Assert-Condition ($demo.enabled -eq $true) "Demo execution is disabled"
    Assert-Condition ($demo.environment -eq "demo") "Environment is not Demo"
    Assert-Condition ($demo.rest_domain -eq "https://api-demo.bybit.com") `
        "Unexpected Bybit REST domain"
    Assert-Condition ($demo.account_verified -eq $true) "Demo account is not verified"
    Assert-Condition ([decimal]$demo.leverage -eq [decimal]1) "Leverage is not 1x"
    Assert-Condition ($demo.kill_switch_active -eq $false) "Demo kill switch is active"
    return $demo
}

if (-not $AllowDemoOrders) {
    [Console]::Error.WriteLine("-AllowDemoOrders is required as explicit human confirmation.")
    exit 1
}
if ($null -eq $MaxNotionalUSDT -or $MaxNotionalUSDT -le [decimal]0) {
    [Console]::Error.WriteLine("MaxNotionalUSDT must be greater than zero and is never increased automatically.")
    exit 1
}
if ($MarketPriceBufferPct -lt [decimal]0 -or $MarketPriceBufferPct -gt [decimal]100) {
    [Console]::Error.WriteLine("MarketPriceBufferPct must be between 0 and 100.")
    exit 1
}

try {
    Set-Location (Split-Path -Parent $PSScriptRoot)
    $python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
    Assert-Condition (Test-Path -LiteralPath $python) "Local .venv Python is missing"

    $databaseUrl = [Environment]::GetEnvironmentVariable("DATABASE_URL", "Process")
    if (-not $databaseUrl) { $databaseUrl = Get-EnvFileValue "DATABASE_URL" }
    Assert-Condition ([bool]$databaseUrl) "DATABASE_URL is not configured"
    $databaseUrl = $databaseUrl -replace "@db:", "@127.0.0.1:"
    $databaseUrl = $databaseUrl -replace "@localhost:", "@127.0.0.1:"

    $script:RunId = "demo-canary-" + [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
    $script:ArtifactDir = Join-Path (Get-Location) "artifacts\demo-canary\$script:RunId"
    New-Item -ItemType Directory -Path $script:ArtifactDir -Force | Out-Null
    $stdoutPath = Join-Path $script:ArtifactDir "uvicorn.stdout.log"
    $stderrPath = Join-Path $script:ArtifactDir "uvicorn.stderr.log"

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
    Set-IsolatedEnvironment "DEMO_CANARY_ENABLED" "true"
    Set-IsolatedEnvironment "BYBIT_PRIVATE_DEMO_BASE_URL" "https://api-demo.bybit.com"
    Set-IsolatedEnvironment "BYBIT_PRIVATE_DEMO_WS_URL" "wss://stream-demo.bybit.com"
    Set-IsolatedEnvironment "DEMO_LEVERAGE" "1"
    Set-IsolatedEnvironment "DEMO_CANARY_MARKET_PRICE_BUFFER_PCT" `
        $MarketPriceBufferPct.ToString([Globalization.CultureInfo]::InvariantCulture)
    Set-IsolatedEnvironment "DEMO_RUN_ID" $script:RunId
    Set-IsolatedEnvironment "DEMO_RUN_STARTED_AT" ([DateTimeOffset]::UtcNow.ToString("o"))
    Set-IsolatedEnvironment "NEWS_ENABLE_RSS" "false"
    Set-IsolatedEnvironment "MARKET_DATA_PROVIDER" "BYBIT_REST"
    Set-IsolatedEnvironment "DATABASE_URL" $databaseUrl

    Invoke-NativeCommand "docker" @("version") "Docker CLI" | Out-Null
    Invoke-NativeCommand "docker" @("compose", "version") "Docker Compose" | Out-Null
    Invoke-NativeCommand "docker" @("compose", "up", "-d", "db") "PostgreSQL start" | Out-Null
    $databaseHealthy = $false
    for ($i = 0; $i -lt 60; $i++) {
        $containerId = (docker compose ps -q db 2>$null)
        if ($LASTEXITCODE -eq 0 -and $containerId) {
            $health = (docker inspect --format '{{.State.Health.Status}}' $containerId 2>$null)
            if ($LASTEXITCODE -eq 0 -and $health -eq "healthy") {
                $databaseHealthy = $true
                break
            }
        }
        Start-Sleep -Seconds 2
    }
    Assert-Condition $databaseHealthy "PostgreSQL did not become healthy"
    Invoke-NativeCommand $python @("-m", "alembic", "upgrade", "head") "Alembic" | Out-Null

    $port = Get-AvailablePort
    $script:BaseUrl = "http://127.0.0.1:$port"
    Start-Uvicorn $python $port $stdoutPath $stderrPath
    Wait-ForApi -TimeoutSeconds $StartupTimeoutSeconds

    $script:FailureStage = "local_preflight"
    $demo = Assert-DemoSafety
    $reconcile = Invoke-Api -Method "POST" -Path "/demo/reconcile"
    Assert-Condition ($reconcile.status -eq "OK") "Demo reconciliation failed"
    Assert-Condition ([int]$reconcile.open_orders_by_symbol.$Symbol -eq 0) `
        "$Symbol has an active order"
    Assert-Condition ([int]$reconcile.remote_positions -eq 0) `
        "Demo account must be flat before the controlled canary"
    $initialUnrelatedOrders = [int]$demo.unrelated_open_orders
    Write-Host "DEMO ACCOUNT VERIFIED: PASS"

    # Preview reads current instrument rules and price without creating a
    # candidate, risk decision, execution reservation, or exchange order.
    $script:FailureStage = "exchange_minimum_preview"
    $preview = Invoke-Api -Method "POST" -Path "/demo/canary/preview" -Body @{
        symbol = $Symbol
        max_notional_usdt = $MaxNotionalUSDT.ToString([Globalization.CultureInfo]::InvariantCulture)
    }
    $plan = $preview.plan
    Assert-Condition ($null -ne $plan) "Exchange minimum plan is missing"
    Assert-Condition ($plan.symbol -eq $Symbol) "Preview returned the wrong symbol"
    Assert-Condition ($plan.instrument_status -eq "Trading") `
        "$Symbol is not Trading"
    Assert-Condition ($null -ne $plan.calculated_quantity) `
        "Calculated minimum order quantity is missing"
    Assert-Condition ([decimal]$plan.calculated_quantity -gt [decimal]0) `
        "Calculated minimum order quantity is invalid"
    Assert-Condition ([decimal]$plan.buffered_required_notional -le $MaxNotionalUSDT) `
        "Buffered required budget exceeds explicit MaxNotionalUSDT"

    Write-Host "DEMO SYMBOL: $($plan.symbol)"
    Write-Host "MIN ORDER QTY: $($plan.min_order_qty)"
    Write-Host "QTY STEP: $($plan.qty_step)"
    Write-Host "MIN NOTIONAL: $($plan.min_notional_value)"
    Write-Host "REFERENCE PRICE: $($plan.reference_price)"
    Write-Host "CALCULATED ORDER QTY: $($plan.calculated_quantity)"
    Write-Host "ESTIMATED NOTIONAL: $($plan.estimated_notional)"
    Write-Host "BUFFERED REQUIRED BUDGET: $($plan.buffered_required_notional)"
    Write-Host "MAX CANARY BUDGET: $($MaxNotionalUSDT.ToString([Globalization.CultureInfo]::InvariantCulture))"
    Write-Host "EXCHANGE MINIMUM VALIDATION: PASS"

    $requiredConfirmation = "SUBMIT $Symbol $($plan.calculated_quantity)"
    if ($AuthorizeCalculatedMinimumQuantity) {
        Assert-Condition ($ExerciseTrailingUpdate -and $AllowDemoOrders) `
            "Non-interactive quantity authorization is restricted to the guarded trailing canary"
        $operatorConfirmation = $requiredConfirmation
    }
    else {
        $operatorConfirmation = Read-Host `
            "Type '$requiredConfirmation' to authorize this exact Demo quantity"
    }
    Assert-Condition ($operatorConfirmation -ceq $requiredConfirmation) `
        "Explicit calculated-quantity confirmation was not provided"

    # Execute re-reads both instrument rules and price. The preview fingerprint
    # makes any intervening rules change fail closed; quantity is the minimum
    # valid quantity, never the entire maximum budget.
    $script:FailureStage = "entry_submission"
    # Once the request crosses the process boundary its outcome is unknown
    # until the durable job is recovered.  A client timeout is not evidence
    # that no candidate, reservation, or exchange order exists.
    $script:NoCandidateCreated = "unknown"
    $script:NoReservationCreated = "unknown"
    $script:NoOrderSubmitted = "unknown"
    $entry = Invoke-Api -Method "POST" -Path "/demo/canary/execute" -Body @{
        symbol = $Symbol
        max_notional_usdt = $MaxNotionalUSDT.ToString([Globalization.CultureInfo]::InvariantCulture)
        expected_rules_fingerprint = [string]$plan.rules_fingerprint
    }
    Assert-Condition ([bool]$entry.job_id) "Canary did not return a durable job ID"
    $jobDeadline = [DateTime]::UtcNow.AddMinutes(3)
    do {
        $job = Invoke-Api -Path "/demo/canary/jobs/$($entry.job_id)"
        if ($job.status -in @("SUCCEEDED", "FAILED")) { break }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $jobDeadline)
    Assert-Condition ($job.status -eq "SUCCEEDED") `
        "Durable canary job did not succeed (status=$($job.status), error=$($job.error_code))"
    $entry = $job.result
    Assert-Condition ($null -ne $entry.execution) "Canary job has no execution result"
    $script:ExecutionId = [string]$entry.execution.id
    $script:NoCandidateCreated = $false
    $script:NoReservationCreated = $false
    $script:NoOrderSubmitted = $false
    $executionId = $script:ExecutionId
    Assert-Condition ([bool]$script:ExecutionId) "Canary execution ID is missing"
    Assert-Condition ([bool]$entry.execution.order_link_id) "orderLinkId is missing"
    Assert-Condition ([bool]$entry.execution.order_id) "Bybit did not accept an entry order ID"
    Assert-Condition ($null -ne $entry.plan) "Final exchange minimum plan is missing"
    Assert-Condition ([decimal]$entry.execution.requested_quantity -eq `
        [decimal]$entry.plan.calculated_quantity) `
        "Submitted quantity differs from the calculated exchange minimum"
    Assert-Condition ($entry.execution.state -in @(
        "DEMO_ORDER_ACKNOWLEDGED", "DEMO_ACCEPTED", "DEMO_PARTIALLY_FILLED",
        "DEMO_FILLED", "DEMO_FULLY_FILLED",
        "DEMO_PROTECTION_PENDING", "DEMO_POSITION_OPEN"
    )) "Demo entry did not reach an accepted state"
    Assert-Condition ($null -ne $entry.execution.risk_decision_id) `
        "Durable risk decision is missing"
    Assert-Condition ([string]$entry.execution.run_id -eq $script:RunId) `
        "Canary execution has the wrong run_id"
    Assert-Condition (@(Get-RunExecutions).Count -eq 1) `
        "Exactly one durable Demo execution was not created"
    $sameOrderLink = @((Invoke-Api -Path "/demo/executions").executions | Where-Object {
        $_.order_link_id -eq $entry.execution.order_link_id
    })
    Assert-Condition ($sameOrderLink.Count -eq 1) "orderLinkId is not unique"
    Write-Host "DEMO ORDER ACKNOWLEDGED: PASS"

    $script:FailureStage = "position_open_confirmation"
    $opened = Wait-ForExecutionState -ExecutionId $executionId `
        -States @("DEMO_POSITION_OPEN")
    Assert-Condition ([decimal]$opened.accepted_quantity -gt 0) `
        "Exchange fill quantity is missing"
    Assert-Condition ([decimal]$opened.average_fill_price -gt 0) `
        "Average fill price is missing"
    Write-Host "DEMO ENTRY FILL CONFIRMED: PASS"
    Write-Host "DEMO POSITION OPEN CONFIRMED: PASS"
    Assert-Condition ($opened.protection_confirmed -eq $true) `
        "Exchange TP/SL was not confirmed"
    Assert-Condition ([decimal]$opened.take_profit -gt 0) "Take profit is missing"
    Assert-Condition ([decimal]$opened.stop_loss -gt 0) "Stop loss is missing"
    Write-Host "DEMO TP/SL VERIFIED: PASS"

    if ($ExerciseTrailingUpdate) {
        $script:FailureStage = "trailing_protection_update"
        $trailing = Invoke-Api -Method "POST" `
            -Path "/demo/canary/$executionId/trailing-update"
        Assert-Condition ($trailing.production_verifier_used -eq $true) `
            "Production trailing verifier was not used"
        Assert-Condition ($trailing.verification.source -eq "REST") `
            "Trailing verification was not authoritative REST"
        Assert-Condition ($trailing.verification.result -in @(
            "VERIFIED", "ALREADY_VERIFIED"
        )) "Trailing protection update was not verified"
        Assert-Condition ([decimal]$trailing.execution.stop_loss -gt 0) `
            "Updated stop loss is missing"
        Write-Host "DEMO TRAILING UPDATE VERIFIED: PASS"
    }

    $script:FailureStage = "restart_reconciliation"
    Stop-Uvicorn
    Start-Uvicorn $python $port $stdoutPath $stderrPath
    Wait-ForApi -TimeoutSeconds $StartupTimeoutSeconds
    $null = Assert-DemoSafety
    $null = Invoke-Api -Method "POST" -Path "/demo/reconcile"
    $restored = Get-Execution -ExecutionId $executionId
    Assert-Condition ($null -ne $restored) "Execution was not restored after restart"
    Assert-Condition ($restored.state -eq "DEMO_POSITION_OPEN") `
        "Exchange position was not reconciled after restart"
    Assert-Condition ($restored.protection_confirmed -eq $true) `
        "TP/SL was not restored after restart"
    Assert-Condition ([decimal]$restored.accepted_quantity -eq [decimal]$opened.accepted_quantity) `
        "Local and remote quantities differ after restart"
    Write-Host "RESTART RECONCILIATION: PASS"

    for ($i = 0; $i -lt 3; $i++) {
        $null = Invoke-Api -Method "POST" -Path "/demo/reconcile"
        Assert-Condition (@(Get-RunExecutions).Count -eq 1) `
            "Canary execution was duplicated"
    }
    Write-Host "IDEMPOTENCY: PASS"

    $script:FailureStage = "planned_close"
    $close = Invoke-Api -Method "POST" `
        -Path "/demo/canary/$executionId/close"
    Assert-Condition ($close.reduce_only -eq $true) `
        "Canary close was not reduce-only"
    $closed = Wait-ForExecutionState -ExecutionId $executionId `
        -States @("DEMO_CLOSED", "DEMO_CLOSED_EXTERNALLY")
    Assert-Condition ([bool]$closed.close_order_id) "Close order ID is missing"
    Assert-Condition (@($closed.close_fills).Count -gt 0) "Close fill was not persisted"
    Assert-Condition ($null -ne $closed.exchange_fees) "Exchange fees are missing"
    Write-Host "DEMO REDUCE-ONLY CLOSE: PASS"

    $finalReconcile = Invoke-Api -Method "POST" -Path "/demo/reconcile"
    $finalDemo = Invoke-Api -Path "/demo/status"
    Assert-Condition ([int]$finalDemo.bot_owned_open_positions -eq 0) `
        "Final Demo position is not flat"
    Assert-Condition ([int]$finalDemo.bot_owned_open_orders -eq 0) `
        "Bot-owned Demo orders remain open"
    Assert-Condition ([int]$finalDemo.unrelated_open_orders -eq $initialUnrelatedOrders) `
        "Unrelated Demo order count changed"
    Write-Host "FINAL DEMO STATE FLAT: PASS"

    $status = Invoke-Api -Path "/status"
    Assert-Condition ($status.live_order_placement_blocked -eq $true) `
        "Live execution is not blocked"
    Assert-Condition ($status.bybit_live_trading_enabled -eq $false) `
        "Live execution flag changed"
    Write-Host "LIVE EXECUTION BLOCKED: PASS"
    $script:FunctionalResult = "PASS"
    $script:SafetyCleanupResult = "PASS"
    $script:FailureStage = $null
    Write-CanaryReport -Status (Get-ExecutionStatus -ExecutionId $executionId) `
        -DemoStatus (Invoke-Api -Path "/demo/status")
    Write-Host "CANARY FUNCTIONAL RESULT: PASS"
    Write-Host "SAFETY CLEANUP RESULT: PASS"
    Write-Host "OVERALL: PASS"
    $script:OverallPassed = $true
}
catch {
    $failureReason = Protect-Text $_.Exception.Message
    [Console]::Error.WriteLine($failureReason)
    $cleanupStatus = $null
    $earlyDemoStatus = $null
    if ($script:BaseUrl) {
        try { $earlyDemoStatus = Invoke-Api -Path "/demo/status" } catch { }
        if (-not $script:ExecutionId -and $script:RunId) {
            try {
                $recoveredJob = Invoke-Api -Path "/demo/canary/jobs/run/$($script:RunId)"
                if ($recoveredJob.execution_id) {
                    $script:ExecutionId = [string]$recoveredJob.execution_id
                    $script:NoCandidateCreated = $false
                    $script:NoReservationCreated = $false
                    $script:NoOrderSubmitted = "unknown"
                }
            } catch { }
        }
    }
    if ($script:ExecutionId -and $script:BaseUrl) {
        try {
            $persistedReason = if ($failureReason -like "*position*open*timeout*" -or
                $failureReason -like "*Timed out waiting for Demo execution state*") {
                "local position-open state timeout"
            } else { $failureReason }
            $null = Invoke-Api -Method "POST" `
                -Path "/demo/canary/$($script:ExecutionId)/failure-cleanup" `
                -Body @{ reason = $persistedReason }
            $deadline = [DateTime]::UtcNow.AddSeconds(90)
            while ([DateTime]::UtcNow -lt $deadline) {
                $null = Invoke-Api -Method "POST" -Path "/demo/reconcile"
                $cleanupStatus = Get-ExecutionStatus -ExecutionId $script:ExecutionId
                if ($cleanupStatus.execution.state -eq "DEMO_CLOSED_AFTER_FAILURE" -and
                    $null -eq $cleanupStatus.remote_position) { break }
                Start-Sleep -Seconds 2
            }
            if ($cleanupStatus -and @($cleanupStatus.execution.fills).Count -gt 0) {
                Write-Host "DEMO ENTRY FILL CONFIRMED DURING CLEANUP: PASS"
            }
            $finalDemo = Invoke-Api -Path "/demo/status"
            $ownCloseFilled = $cleanupStatus -and
                [bool]$cleanupStatus.execution.close_order_id -and
                @($cleanupStatus.execution.close_fills | Where-Object {
                    $_.order_id -eq $cleanupStatus.execution.close_order_id
                }).Count -gt 0
            $terminalCleanup = $cleanupStatus.execution.state -in @(
                "DEMO_CLOSED", "DEMO_CLOSED_AFTER_FAILURE",
                "DEMO_CLOSED_AFTER_INTERRUPTION", "DEMO_CLOSED_EXTERNALLY",
                "DEMO_FAILED_FLAT_VERIFIED"
            )
            if ($ownCloseFilled -and $terminalCleanup -and
                [int]$finalDemo.bot_owned_open_positions -eq 0 -and
                [int]$finalDemo.bot_owned_open_orders -eq 0) {
                $script:SafetyCleanupResult = "PASS"
                Write-Host "DEMO REDUCE-ONLY CLEANUP CLOSE: PASS"
                Write-Host "FINAL DEMO STATE FLAT: PASS"
            } else {
                $script:SafetyCleanupResult = "IN_PROGRESS"
                Write-Host "DEMO REDUCE-ONLY CLEANUP CLOSE: IN_PROGRESS"
            }
            Write-CanaryReport -Status $cleanupStatus -FailureReason $persistedReason `
                -DemoStatus $finalDemo
        }
        catch {
            $script:SafetyCleanupResult = "FAIL"
            Write-Warning (Protect-Text "Safety cleanup failed: $($_.Exception.Message)")
            if ($script:ExecutionId) {
                try {
                    Invoke-NativeCommand $python @(
                        "scripts/demo_direct_cleanup.py",
                        "--execution-id", $script:ExecutionId,
                        "--confirm-cleanup"
                    ) "Direct guarded Demo cleanup" | Out-Null
                    $script:SafetyCleanupResult = "PASS"
                    Write-Host "DIRECT GUARDED DEMO CLEANUP: PASS"
                }
                catch {
                    Write-Warning (Protect-Text "Direct cleanup failed: $($_.Exception.Message)")
                }
            }
        }
    }
    if (-not $script:ReportWritten) {
        Write-CanaryReport -Status $null -FailureReason $failureReason `
            -DemoStatus $earlyDemoStatus
    }
    Write-Host "CANARY FUNCTIONAL RESULT: FAIL"
    Write-Host "SAFETY CLEANUP RESULT: $($script:SafetyCleanupResult)"
    Write-Host "OVERALL: FAIL"
    if ($stderrPath) {
        $diagnostic = Protect-Text (Safe-ReadTextFile -Path $stderrPath)
        if ($diagnostic) { Write-Warning ($diagnostic -split "`r?`n" | Select-Object -Last 20 | Out-String) }
    }
}
finally {
    Stop-Uvicorn
    Restore-Environment
}

if (-not $script:OverallPassed) { exit 1 }
exit 0
