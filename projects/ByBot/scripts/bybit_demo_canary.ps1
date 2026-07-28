param(
    [switch]$AllowDemoOrders,
    [ValidateSet("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")]
    [string]$Symbol = "BTCUSDT",
    [Nullable[decimal]]$MaxNotionalUSDT = $null,
    [decimal]$MarketPriceBufferPct = 5,
    [switch]$ExerciseTrailingUpdate,
    [switch]$ExerciseMarketInvalidation,
    [switch]$InjectMonitorStatusTimeout,
    [switch]$ExerciseFlatDuringProtectionRace,
    [switch]$EnterDrainBeforeFlatRace,
    [switch]$ExerciseDepthGate,
    [switch]$ExercisePriceGate,
    [ValidateSet(0, 100, 200)]
    [int]$V2SizingTier = 0,
    [switch]$SkipControlledRestart,
    [switch]$AuthorizeCalculatedMinimumQuantity,
    [switch]$ValidateLotGuardOnly,
    [decimal]$LotReferencePrice = 0,
    [decimal]$LotMinOrderQty = 0,
    [decimal]$LotQtyStep = 0,
    [decimal]$LotMinNotionalUSDT = 0,
    [decimal]$LotTargetNotionalUSDT = 0,
    [decimal]$LotAcceptedQuantity = 0,
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
$script:MarketReadiness = $null
$script:PhaseBeforeRestart = $null
$script:PhaseImmediatelyAfterRestart = $null
$script:FinalPhase = $null
$script:ExecutionsBeforeRestart = $null
$script:ExecutionsAfterRestart = $null
$script:AdmissionsBeforeRestart = $null
$script:AdmissionsAfterRestart = $null
$script:ProtectionInvalidation = $null
$script:MonitorDegradation = $null

function Assert-Condition {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Get-QuantityCeiling {
    param([decimal]$Quantity, [decimal]$Step)
    Assert-Condition ($Step -gt 0) "Quantity step must be positive"
    return [decimal]::Ceiling($Quantity / $Step) * $Step
}

function Get-QuantityFloor {
    param([decimal]$Quantity, [decimal]$Step)
    Assert-Condition ($Step -gt 0) "Quantity step must be positive"
    return [decimal]::Floor($Quantity / $Step) * $Step
}

function Get-ExchangeLotAwareCanaryGuard {
    param(
        [decimal]$TargetNotional,
        [decimal]$ReferencePrice,
        [decimal]$MinOrderQty,
        [decimal]$QtyStep,
        [decimal]$MinNotional,
        [decimal]$AcceptedQuantity,
        [decimal]$ExplicitMaximumNotional
    )
    Assert-Condition ($TargetNotional -gt 0) "Target notional must be positive"
    Assert-Condition ($ReferencePrice -gt 0) "Executable reference price must be positive"
    Assert-Condition ($MinOrderQty -gt 0) "Exchange minimum quantity must be positive"
    Assert-Condition ($QtyStep -gt 0) "Exchange quantity step must be positive"
    Assert-Condition ($MinNotional -ge 0) "Exchange minimum notional cannot be negative"
    Assert-Condition ($AcceptedQuantity -gt 0) "Accepted quantity must be positive"
    Assert-Condition ($ExplicitMaximumNotional -gt 0) "Explicit maximum notional must be positive"

    $minimumByNotional = Get-QuantityCeiling `
        ($MinNotional / $ReferencePrice) $QtyStep
    $roundedMinimumQuantity = Get-QuantityCeiling $MinOrderQty $QtyStep
    $minimumValidQuantity = if (
        $roundedMinimumQuantity -ge $minimumByNotional
    ) { $roundedMinimumQuantity } else { $minimumByNotional }
    $lowerTargetQuantity = Get-QuantityFloor `
        ($TargetNotional / $ReferencePrice) $QtyStep
    $upperTargetQuantity = Get-QuantityCeiling `
        ($TargetNotional / $ReferencePrice) $QtyStep
    $nearestSafeQuantity = if (
        $minimumValidQuantity -ge $upperTargetQuantity
    ) { $minimumValidQuantity } else { $upperTargetQuantity }
    $acceptedNotional = $AcceptedQuantity * $ReferencePrice
    $oneStepNotional = $QtyStep * $ReferencePrice

    Assert-Condition ($AcceptedQuantity -ge $minimumValidQuantity) `
        "Accepted quantity is below the exchange minimum"
    Assert-Condition (
        (($AcceptedQuantity / $QtyStep) % [decimal]1) -eq [decimal]0
    ) "Accepted quantity is not aligned to qtyStep"
    Assert-Condition ($AcceptedQuantity -eq $nearestSafeQuantity) `
        "Accepted quantity is not the nearest safe exchange-valid quantity"
    Assert-Condition ($acceptedNotional -le $ExplicitMaximumNotional) `
        "Accepted notional exceeds explicit MaxNotionalUSDT"

    return [ordered]@{
        target_notional = $TargetNotional
        executable_reference_price = $ReferencePrice
        minimum_valid_quantity = $minimumValidQuantity
        lower_target_quantity = $lowerTargetQuantity
        nearest_valid_quantity = $nearestSafeQuantity
        normalized_accepted_quantity = $AcceptedQuantity
        accepted_notional = $acceptedNotional
        qty_step = $QtyStep
        one_qty_step_notional = $oneStepNotional
        explicit_maximum_notional = $ExplicitMaximumNotional
    }
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

function Write-ControllerEvent {
    param(
        [string]$Event,
        [string]$Stage,
        [datetimeoffset]$StartedAt,
        [int]$TimeoutSeconds,
        [string]$Status = $null,
        [string]$ErrorMessage = $null
    )
    if (-not $script:ControllerEventsPath) { return }
    $now = [DateTimeOffset]::UtcNow
    $payload = [ordered]@{
        event = $Event
        stage = $Stage
        occurred_at = $now.ToString("o")
        started_at = $StartedAt.ToString("o")
        elapsed_seconds = [Math]::Round(($now - $StartedAt).TotalSeconds, 3)
        timeout_seconds = $TimeoutSeconds
        status = $Status
        error = Protect-Text $ErrorMessage
        controller_pid = $PID
        uvicorn_pid = if ($script:Child) { $script:Child.Id } else { $null }
    }
    Add-Content -LiteralPath $script:ControllerEventsPath -Value (
        $payload | ConvertTo-Json -Compress
    ) -Encoding UTF8
}

function Invoke-ControllerStep {
    param(
        [string]$Stage,
        [int]$TimeoutSeconds,
        [scriptblock]$Action
    )
    $started = [DateTimeOffset]::UtcNow
    Write-ControllerEvent "CONTROLLER_STEP_STARTED" $Stage $started $TimeoutSeconds
    try {
        $result = & $Action
        $elapsed = ([DateTimeOffset]::UtcNow - $started).TotalSeconds
        if ($elapsed -gt $TimeoutSeconds) {
            throw "$Stage exceeded its bounded timeout of $TimeoutSeconds seconds"
        }
        Write-ControllerEvent "CONTROLLER_STEP_FINISHED" $Stage $started `
            $TimeoutSeconds "PASS"
        return $result
    }
    catch {
        Write-ControllerEvent "CONTROLLER_STEP_FINISHED" $Stage $started `
            $TimeoutSeconds "FAIL" $_.Exception.Message
        throw
    }
}

function Invoke-NativeCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$Label,
        [int]$TimeoutSeconds = 120
    )
    $stdoutPath = [IO.Path]::GetTempFileName()
    $stderrPath = [IO.Path]::GetTempFileName()
    $process = $null
    try {
        $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments `
            -NoNewWindow -PassThru -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        # Retain the real native process handle on Windows PowerShell 5.1.
        [void]$process.Handle
        $exited = $process.WaitForExit($TimeoutSeconds * 1000)
        if (-not $exited) {
            & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
            $process.WaitForExit()
            $process.Refresh()
            throw "$Label timed out after $TimeoutSeconds seconds"
        }
        # Windows PowerShell 5.1 can expose a stale/null ExitCode after a
        # timed WaitForExit when output is redirected.
        $process.WaitForExit()
        $process.Refresh()
        $exitCode = $process.ExitCode
        if ($null -eq $exitCode) {
            throw "$Label exit code was not captured"
        }
        $stdout = [IO.File]::ReadAllText($stdoutPath)
        $stderr = [IO.File]::ReadAllText($stderrPath)
        if ($stdout) { Write-Host (Protect-Text $stdout.TrimEnd()) }
        # Native progress output on stderr is not a PowerShell error when exit=0.
        if ($stderr) { Write-Host (Protect-Text $stderr.TrimEnd()) }
        if ($exitCode -ne 0) { throw "$Label failed with exit code $exitCode" }
        return $stdout
    }
    finally {
        if ($process) { $process.Dispose() }
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

if ($ValidateLotGuardOnly) {
    try {
        $validation = Get-ExchangeLotAwareCanaryGuard `
            -TargetNotional $LotTargetNotionalUSDT `
            -ReferencePrice $LotReferencePrice `
            -MinOrderQty $LotMinOrderQty `
            -QtyStep $LotQtyStep `
            -MinNotional $LotMinNotionalUSDT `
            -AcceptedQuantity $LotAcceptedQuantity `
            -ExplicitMaximumNotional $MaxNotionalUSDT
        $validation | ConvertTo-Json -Compress
        exit 0
    }
    catch {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }
}

function Stop-Uvicorn {
    if ($null -eq $script:Child) { return }
    $rootPid = $script:Child.Id
    try {
        if (-not $script:Child.HasExited) {
            $script:Child.CloseMainWindow() | Out-Null
            if (-not $script:Child.WaitForExit(5000)) {
                & taskkill.exe /PID $rootPid /T /F 2>$null | Out-Null
            }
        }
        if (-not $script:Child.HasExited) {
            $script:Child.WaitForExit()
        }
        else {
            $script:Child.WaitForExit()
        }
        $script:Child.Refresh()
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
        $responseBody = $null
        if ($_.Exception.Response) {
            $status = [int]$_.Exception.Response.StatusCode
            try {
                $stream = $_.Exception.Response.GetResponseStream()
                if ($stream) {
                    $reader = New-Object IO.StreamReader($stream)
                    try { $responseBody = $reader.ReadToEnd() }
                    finally { $reader.Dispose() }
                }
            }
            catch { }
        }
        $detail = if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            Protect-Text $_.ErrorDetails.Message
        }
        elseif ($responseBody) {
            Protect-Text $responseBody
        }
        else {
            $_.Exception.Message
        }
        throw "API $Method $Path failed (HTTP $status): $detail"
    }
}

function Invoke-DemoReconcile {
    param([int]$Attempts = 3)
    $lastFailure = $null
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            return Invoke-Api -Method "POST" -Path "/demo/reconcile" -TimeoutSec 90
        }
        catch {
            $lastFailure = $_
            if ($script:Child -and $script:Child.HasExited) {
                throw
            }
            if ($attempt -lt $Attempts) {
                Start-Sleep -Seconds (2 * $attempt)
            }
        }
    }
    throw $lastFailure
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

function Start-UvicornReady {
    param(
        [string]$Python,
        [int]$Port,
        [string]$Stdout,
        [string]$Stderr,
        [int]$TimeoutSeconds,
        [int]$Attempts = 3
    )
    $lastFailure = $null
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        Start-Uvicorn $Python $Port $Stdout $Stderr
        try {
            Wait-ForApi -TimeoutSeconds $TimeoutSeconds
            return
        }
        catch {
            $lastFailure = $_
            Stop-Uvicorn
            if ($attempt -lt $Attempts) {
                Start-Sleep -Seconds (2 * $attempt)
            }
        }
    }
    throw $lastFailure
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
        active_kill_switch_reasons = if ($DemoStatus) {
            @($DemoStatus.active_kill_switch_reasons)
        } else { @() }
        historical_kill_switch_reasons = if ($DemoStatus) {
            @($DemoStatus.historical_kill_switch_reasons)
        } else { @() }
        # Backward-compatible field now means active blockers only.
        kill_switch_reasons = if ($DemoStatus) {
            @($DemoStatus.active_kill_switch_reasons)
        } else { @() }
        market_readiness = $script:MarketReadiness
        phase_before_restart = $script:PhaseBeforeRestart
        phase_immediately_after_restart = $script:PhaseImmediatelyAfterRestart
        final_phase = $script:FinalPhase
        entries_submitted_after_restart = if (
            $null -ne $script:ExecutionsBeforeRestart -and
            $null -ne $script:ExecutionsAfterRestart
        ) {
            [Math]::Max(
                0,
                [int]$script:ExecutionsAfterRestart -
                [int]$script:ExecutionsBeforeRestart
            )
        } else { $null }
        entries_admitted_after_restart = if (
            $null -ne $script:AdmissionsBeforeRestart -and
            $null -ne $script:AdmissionsAfterRestart
        ) {
            [Math]::Max(
                0,
                [int]$script:AdmissionsAfterRestart -
                [int]$script:AdmissionsBeforeRestart
            )
        } else { $null }
        no_candidate_created = $script:NoCandidateCreated
        no_reservation_created = $script:NoReservationCreated
        no_order_submitted = $script:NoOrderSubmitted
        protection_invalidation = $script:ProtectionInvalidation
        monitor_degradation = $script:MonitorDegradation
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
if ($EnterDrainBeforeFlatRace -and (
    -not $ExerciseFlatDuringProtectionRace -or -not $V2SizingTier
)) {
    [Console]::Error.WriteLine(
        "EnterDrainBeforeFlatRace requires the V2 flat-protection race canary."
    )
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
    $script:ControllerEventsPath = Join-Path $script:ArtifactDir "controller-events.jsonl"
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
    Set-IsolatedEnvironment "V2_ENABLED" $(if ($V2SizingTier) { "true" } else { "false" })
    Set-IsolatedEnvironment "V2_AUTO_DEMO_EXECUTION" "false"
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

    Invoke-ControllerStep "docker_cli" 30 {
        Invoke-NativeCommand "docker" @("version") "Docker CLI" 30 | Out-Null
    } | Out-Null
    Invoke-ControllerStep "docker_compose" 30 {
        Invoke-NativeCommand "docker" @("compose", "version") "Docker Compose" 30 |
            Out-Null
    } | Out-Null
    Invoke-ControllerStep "postgres_start" 120 {
        Invoke-NativeCommand "docker" @("compose", "up", "-d", "db") `
            "PostgreSQL start" 120 | Out-Null
    } | Out-Null
    Invoke-ControllerStep "postgres_health" 125 {
        $databaseHealthy = $false
        for ($i = 0; $i -lt 60; $i++) {
            $containerId = (docker compose ps -q db 2>$null)
            if ($LASTEXITCODE -eq 0 -and $containerId) {
                $health = (
                    docker inspect --format '{{.State.Health.Status}}' `
                        $containerId 2>$null
                )
                if ($LASTEXITCODE -eq 0 -and $health -eq "healthy") {
                    $databaseHealthy = $true
                    break
                }
            }
            Start-Sleep -Seconds 2
        }
        Assert-Condition $databaseHealthy "PostgreSQL did not become healthy"
    } | Out-Null
    Invoke-ControllerStep "alembic_upgrade" 120 {
        Invoke-NativeCommand $python @("-m", "alembic", "upgrade", "head") `
            "Alembic" 120 | Out-Null
    } | Out-Null

    $port = Get-AvailablePort
    $script:BaseUrl = "http://127.0.0.1:$port"
    Invoke-ControllerStep "uvicorn_readiness" ($StartupTimeoutSeconds + 15) {
        Start-UvicornReady $python $port $stdoutPath $stderrPath `
            $StartupTimeoutSeconds
    } | Out-Null

    $script:FailureStage = "local_preflight"
    $localPreflight = Invoke-ControllerStep "local_demo_preflight" 180 {
        [pscustomobject]@{
            demo = Assert-DemoSafety
            reconcile = Invoke-DemoReconcile
        }
    }
    $demo = $localPreflight.demo
    $reconcile = $localPreflight.reconcile
    Assert-Condition ($reconcile.status -eq "OK") "Demo reconciliation failed"
    Assert-Condition ([int]$reconcile.open_orders_by_symbol.$Symbol -eq 0) `
        "$Symbol has an active order"
    Assert-Condition ([int]$reconcile.remote_positions -eq 0) `
        "Demo account must be flat before the controlled canary"
    if ($V2SizingTier) {
        $v2Preflight = Invoke-ControllerStep "v2_execution_preflight" 120 {
            Invoke-Api -Path "/v2/preflight" -TimeoutSec 90
        }
        Assert-Condition ($v2Preflight.ok -eq $true) (
            "V2 execution preflight failed: " +
            (@($v2Preflight.blockers) -join "; ")
        )
        Write-Host "V2 EXECUTION PREFLIGHT: PASS"
    }
    if ($ExerciseDepthGate -or $ExercisePriceGate) {
        Assert-Condition ($V2SizingTier -eq 100) `
            "Pre-submit gate canary requires V2SizingTier 100"
        $gateQuery = if ($ExercisePriceGate) { "?gate=price" } else { "" }
        $depthGate = Invoke-Api -Method "POST" `
            -Path "/v2/canary/depth-gate/${Symbol}${gateQuery}" -TimeoutSec 90
        $expectedRejection = if ($ExercisePriceGate) {
            "FINAL_PRICE_MOVED_BEYOND_TOLERANCE"
        } else {
            "FINAL_EXECUTABLE_DEPTH_INSUFFICIENT"
        }
        Assert-Condition (
            $depthGate.rejection_code -eq $expectedRejection
        ) "Stage A did not produce the expected pre-submit rejection"
        Assert-Condition ($depthGate.execution_created -eq $false) `
            "Stage A created a durable Demo execution"
        Assert-Condition (
            $depthGate.exchange_order_submission_invoked -eq $false
        ) "Stage A invoked exchange order submission"
        Assert-Condition (
            $depthGate.reservation_release_result -eq "RELEASED" -and
            $depthGate.reservation_state -eq "RELEASED"
        ) "Stage A reservation was not released exactly once"
        Assert-Condition (
            [int]$depthGate.cycle_failures_after -eq
            [int]$depthGate.cycle_failures_before
        ) "Stage A created a cycle failure"
        Write-Host "PRE-SUBMIT GATE STAGE A REJECTION: PASS"
        Write-Host "PRE-SUBMIT GATE STAGE A RESERVATION RELEASE: PASS"
    }
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
    $script:MarketReadiness = $preview.market_readiness
    Assert-Condition ($null -ne $script:MarketReadiness) `
        "Market-data readiness report is missing"
    Assert-Condition (
        $script:MarketReadiness.source -in @("WS", "REST")
    ) "Canary market-data source is not authoritative"
    Assert-Condition (
        [decimal]$script:MarketReadiness.age_seconds -ge [decimal]0
    ) "Canary market snapshot age is invalid"
    Write-Host "CANARY MARKET SOURCE: $($script:MarketReadiness.source)"
    Write-Host "CANARY MARKET EXCHANGE TIMESTAMP: $($script:MarketReadiness.exchange_timestamp)"
    Write-Host "CANARY MARKET RECEIVED AT: $($script:MarketReadiness.received_at)"
    Write-Host "CANARY MARKET AGE SECONDS: $($script:MarketReadiness.age_seconds)"
    Write-Host "CANARY MARKET READINESS WAIT SECONDS: $($script:MarketReadiness.waited_seconds)"
    Write-Host "CANARY MARKET READINESS: PASS"
    Assert-Condition ($null -ne $plan) "Exchange minimum plan is missing"
    Assert-Condition ($plan.symbol -eq $Symbol) "Preview returned the wrong symbol"
    Assert-Condition ($plan.instrument_status -eq "Trading") `
        "$Symbol is not Trading"
    Assert-Condition ($null -ne $plan.calculated_quantity) `
        "Calculated minimum order quantity is missing"
    Assert-Condition ([decimal]$plan.calculated_quantity -gt [decimal]0) `
        "Calculated minimum order quantity is invalid"
    if (-not $V2SizingTier) {
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
    }

    $requiredConfirmation = if ($V2SizingTier) {
        "SUBMIT $Symbol V2 $V2SizingTier"
    } else {
        "SUBMIT $Symbol $($plan.calculated_quantity)"
    }
    if ($AuthorizeCalculatedMinimumQuantity) {
        Assert-Condition (
            ($ExerciseTrailingUpdate -or $ExerciseFlatDuringProtectionRace -or
             $V2SizingTier) -and
            $AllowDemoOrders
        ) "Non-interactive quantity authorization is restricted to a guarded protection canary"
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
    if ($V2SizingTier) {
        $v2Result = Invoke-Api -Method "POST" `
            -Path "/v2/canary/sizing/$Symbol/$V2SizingTier" -TimeoutSec 90
        Assert-Condition ($v2Result.production_v2_sizing_used -eq $true) `
            "Production V2 sizing was not used"
        Assert-Condition ($null -ne $v2Result.sizing) "V2 sizing audit is missing"
        $entry = @{
            execution = (Get-Execution -ExecutionId $v2Result.execution_id)
            sizing = $v2Result.sizing
        }
        Assert-Condition ($null -ne $entry.execution) "V2 canary execution is missing"
    }
    else {
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
    }
    $script:ExecutionId = [string]$entry.execution.id
    $script:NoCandidateCreated = $false
    $script:NoReservationCreated = $false
    $script:NoOrderSubmitted = $false
    $executionId = $script:ExecutionId
    Assert-Condition ([bool]$script:ExecutionId) "Canary execution ID is missing"
    Assert-Condition ([bool]$entry.execution.order_link_id) "orderLinkId is missing"
    Assert-Condition ([bool]$entry.execution.order_id) "Bybit did not accept an entry order ID"
    if ($V2SizingTier) {
        $lotGuard = Get-ExchangeLotAwareCanaryGuard `
            -TargetNotional ([decimal]$entry.sizing.requested_notional_usdt) `
            -ReferencePrice ([decimal]$entry.execution.reference_entry_price) `
            -MinOrderQty ([decimal]$plan.min_order_qty) `
            -QtyStep ([decimal]$plan.qty_step) `
            -MinNotional ([decimal]$plan.min_notional_value) `
            -AcceptedQuantity ([decimal]$entry.execution.requested_quantity) `
            -ExplicitMaximumNotional $MaxNotionalUSDT
        Assert-Condition (
            [decimal]$entry.sizing.normalized_accepted_quantity -eq
            [decimal]$lotGuard.normalized_accepted_quantity
        ) "Production sizing quantity differs from the exchange-lot guard"
        Assert-Condition (
            [decimal]$entry.sizing.normalized_accepted_notional_usdt -le
            [decimal]$entry.sizing.symbol_cap_usdt
        ) "Normalized V2 notional exceeds the production symbol cap"
        Assert-Condition (
            [decimal]$entry.sizing.normalized_accepted_notional_usdt -le
            [decimal]$entry.sizing.portfolio_remaining_capacity_usdt
        ) "Normalized V2 notional exceeds production portfolio capacity"
        Write-Host "V2 TARGET NOTIONAL: $($lotGuard.target_notional)"
        Write-Host "V2 EXECUTABLE REFERENCE PRICE: $($lotGuard.executable_reference_price)"
        Write-Host "V2 MINIMUM VALID QUANTITY: $($lotGuard.minimum_valid_quantity)"
        Write-Host "V2 NEAREST VALID QUANTITY: $($lotGuard.nearest_valid_quantity)"
        Write-Host "V2 NORMALIZED ACCEPTED QUANTITY: $($lotGuard.normalized_accepted_quantity)"
        Write-Host "V2 ACCEPTED NOTIONAL: $($lotGuard.accepted_notional)"
        Write-Host "V2 QTY STEP: $($lotGuard.qty_step)"
        Write-Host "V2 ONE QTY STEP NOTIONAL: $($lotGuard.one_qty_step_notional)"
        Write-Host "V2 EXCHANGE-LOT GUARD: PASS"
    }
    else {
        Assert-Condition ($null -ne $entry.plan) "Final exchange minimum plan is missing"
        Assert-Condition ([decimal]$entry.execution.requested_quantity -eq `
            [decimal]$entry.plan.calculated_quantity) `
            "Submitted quantity differs from the calculated exchange minimum"
    }
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
    if ($V2SizingTier) {
        $actualAcceptedNotional = [decimal]$opened.accepted_quantity * `
            [decimal]$opened.average_fill_price
        Assert-Condition (
            [decimal]$opened.accepted_quantity -eq
            [decimal]$lotGuard.normalized_accepted_quantity
        ) "Actual accepted quantity differs from the guarded exchange lot"
        Assert-Condition ($actualAcceptedNotional -le $MaxNotionalUSDT) `
            "Actual accepted V2 notional exceeds explicit MaxNotionalUSDT"
        Write-Host "V2 ACTUAL ACCEPTED NOTIONAL: $actualAcceptedNotional USDT"
    }
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
    elseif ($ExerciseFlatDuringProtectionRace) {
        $script:FailureStage = "flat_during_protection_race"
        if ($EnterDrainBeforeFlatRace) {
            $drain = Invoke-Api -Method "POST" -Path "/v2/stop-new-entries"
            Assert-Condition ($drain.run_phase -eq "DRAINING") `
                "V2 runtime did not enter DRAINING before the exact close"
            $script:PhaseBeforeRestart = [string]$drain.run_phase
            Write-Host "DEMO DRAINING BEFORE CLOSE: PASS"
        }
        $race = Invoke-Api -Method "POST" `
            -Path "/demo/canary/$executionId/flat-during-protection-race" `
            -TimeoutSec 90
        Assert-Condition ($race.production_verifier_used -eq $true) `
            "Production protection verifier was not used"
        Assert-Condition ($race.cycle_failure_emitted -eq $false) `
            "Benign flat-position race emitted a cycle failure"
        Assert-Condition ($race.classification -eq "TERMINALIZATION_HANDOFF") `
            "Flat-position race did not use TERMINALIZATION_HANDOFF"
        Assert-Condition ($race.execution.state -in @(
            "DEMO_CLOSED", "DEMO_CLOSED_EXTERNALLY"
        )) "Flat-position race did not reach a terminal state"
        Assert-Condition ([bool]$race.execution.close_order_id) `
            "Exact race close order ID is missing"
        Assert-Condition (@($race.execution.close_fills).Count -gt 0) `
            "Exact race close fill is missing"
        Assert-Condition ($race.execution.exit_attribution -eq "strategy_exit") `
            "Post-close protection canary did not use strategy_exit attribution"
        Write-Host "DEMO FLAT-DURING-PROTECTION HANDOFF: PASS"
    }

    if ($ExerciseMarketInvalidation) {
        Assert-Condition $ExerciseTrailingUpdate `
            "Market invalidation canary requires a prior valid trailing update"
        $script:FailureStage = "market_invalidated_protection_update"
        $invalidated = Invoke-Api -Method "POST" `
            -Path "/demo/canary/$executionId/market-invalidated-protection" `
            -TimeoutSec 90
        Assert-Condition ($invalidated.production_verifier_used -eq $true) `
            "Production protection verifier was not used"
        Assert-Condition (
            $invalidated.classification -eq
            "PROTECTION_UPDATE_INVALIDATED_BY_MARKET"
        ) "Market-crossed update used the wrong classification"
        Assert-Condition ($invalidated.cycle_failure_emitted -eq $false) `
            "Market-crossed update emitted a cycle failure"
        Assert-Condition ($invalidated.stale_value_retried -eq $false) `
            "Stale protection value was retried"
        Assert-Condition (
            $invalidated.verification.classification -eq
            "POSITION_OPEN_EXISTING_PROTECTION_CONFIRMED"
        ) "Existing protection was not authoritatively confirmed"
        Assert-Condition (
            $invalidated.verification.source -eq "REST"
        ) "Market invalidation verification was not authoritative REST"
        $script:ProtectionInvalidation = $invalidated
        Write-Host "PROTECTION UPDATE INVALIDATED BY MARKET: PASS"
        Write-Host "EXISTING PROTECTION RETAINED: PASS"
    }

    if ($InjectMonitorStatusTimeout) {
        $script:FailureStage = "certification_monitor_degradation"
        $monitorDir = Join-Path $script:ArtifactDir "certification-monitor"
        New-Item -ItemType Directory -Path $monitorDir -Force | Out-Null
        Invoke-NativeCommand $python @(
            "scripts/demo_v2_certification_monitor.py",
            "--run-id", $script:RunId,
            "--runner-pid", "$PID",
            "--uvicorn-pid", "$($script:Child.Id)",
            "--base-url", $script:BaseUrl,
            "--output-dir", $monitorDir,
            "--hard-timeout-seconds", "30",
            "--idle-poll-seconds", "1",
            "--active-poll-seconds", "1",
            "--drain-poll-seconds", "1",
            "--retry-poll-seconds", "1",
            "--inject-status-timeouts", "1",
            "--max-polls", "2"
        ) "Certification monitor degradation canary" 60 | Out-Null
        $monitorResult = Get-Content -Raw (
            Join-Path $monitorDir "monitor-result.json"
        ) | ConvertFrom-Json
        Assert-Condition (
            $monitorResult.result -eq "OBSERVATION_COMPLETE"
        ) "Certification monitor did not complete its bounded observation"
        Assert-Condition (
            $monitorResult.monitor_health.state -eq "HEALTHY"
        ) "Certification monitor did not recover to HEALTHY"
        Assert-Condition (
            [int]$monitorResult.monitor_health.incident_count -eq 1 -and
            [int]$monitorResult.monitor_health.recovered_count -eq 1
        ) "Certification monitor degradation/recovery count is incorrect"
        $script:MonitorDegradation = $monitorResult
        Write-Host "CERTIFICATION MONITOR STATUS_DEGRADED: PASS"
        Write-Host "CERTIFICATION MONITOR RECOVERED: PASS"
    }

    $script:FailureStage = "restart_reconciliation"
    if ($SkipControlledRestart) {
        Assert-Condition ($V2SizingTier -in @(100, 200)) `
            "Controlled restart may be skipped only by the guarded V2 sizing canary"
        $restored = Get-Execution -ExecutionId $executionId
        Write-Host "SIZING-ONLY CONTROLLED RESTART: NOT REQUIRED"
    }
    else {
        $preRestartV2 = Invoke-Api -Path "/v2/status"
        $script:AdmissionsBeforeRestart = [int](
            $preRestartV2.signal_metrics.admitted_signals
        )
        $script:ExecutionsBeforeRestart = @(Get-RunExecutions).Count
        Stop-Uvicorn
        Start-UvicornReady $python $port $stdoutPath $stderrPath `
            $StartupTimeoutSeconds
        $restartV2 = Invoke-Api -Path "/v2/status"
        $script:PhaseImmediatelyAfterRestart = [string]$restartV2.run_phase
        if ($EnterDrainBeforeFlatRace) {
            Assert-Condition (
                $script:PhaseImmediatelyAfterRestart -ne "RUNNING"
            ) "V2 runtime phase regressed to RUNNING after restart"
        }
        $null = Assert-DemoSafety
        $null = Invoke-DemoReconcile
        $restored = Get-Execution -ExecutionId $executionId
        $script:ExecutionsAfterRestart = @(Get-RunExecutions).Count
    }
    Assert-Condition ($null -ne $restored) "Execution was not restored after restart"
    $closedDuringRestart = $false
    if ($ExerciseFlatDuringProtectionRace) {
        Assert-Condition ($restored.state -in @(
            "DEMO_CLOSED", "DEMO_CLOSED_EXTERNALLY"
        )) "Terminal race execution was not restored after restart"
        Assert-Condition (@($restored.close_fills).Count -gt 0) `
            "Terminal race close fill was not restored"
    }
    elseif ($restored.state -in @("DEMO_CLOSED", "DEMO_CLOSED_EXTERNALLY")) {
        Assert-Condition (@($restored.close_fills).Count -gt 0) `
            "Terminal restart close has no exact attributed fill"
        Assert-Condition (
            $restored.close_reason -in @("take_profit", "stop_loss")
        ) "Restart terminal state was not produced by verified TP/SL"
        Assert-Condition (
            $restored.cleanup_result -eq
            "remote position flat and bot-owned orders zero"
        ) "Restart terminal state is not authoritatively flat"
        $closedDuringRestart = $true
        Write-Host "NATURAL CLOSE DURING RESTART: PASS"
    }
    else {
        Assert-Condition ($restored.state -eq "DEMO_POSITION_OPEN") `
            "Exchange position was not reconciled after restart"
        Assert-Condition ($restored.protection_confirmed -eq $true) `
            "TP/SL was not restored after restart"
    }
    Assert-Condition ([decimal]$restored.accepted_quantity -eq [decimal]$opened.accepted_quantity) `
        "Local and remote quantities differ after restart"
    if (-not $SkipControlledRestart) {
        Write-Host "RESTART RECONCILIATION: PASS"
    }

    for ($i = 0; $i -lt 3; $i++) {
        $null = Invoke-DemoReconcile
        Assert-Condition (@(Get-RunExecutions).Count -eq 1) `
            "Canary execution was duplicated"
    }
    Write-Host "IDEMPOTENCY: PASS"

    if (-not $ExerciseFlatDuringProtectionRace -and -not $closedDuringRestart) {
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
        Assert-Condition ($closed.accounting_status -eq "FINAL") `
            "Fill-level accounting is not final"
        Assert-Condition (
            [decimal]$closed.realized_exchange_pnl -eq
            [decimal]$closed.authoritative_closed_pnl
        ) "Durable PnL does not equal authoritative exchange PnL"
        Write-Host "FILL-LEVEL PNL: PASS"
        Write-Host "DEMO REDUCE-ONLY CLOSE: PASS"
    }

    $finalReconcile = Invoke-DemoReconcile
    if ($EnterDrainBeforeFlatRace) {
        $finalV2 = Invoke-Api -Path "/v2/status"
        $script:FinalPhase = [string]$finalV2.run_phase
        $script:AdmissionsAfterRestart = [int](
            $finalV2.signal_metrics.admitted_signals
        )
        Assert-Condition ($finalV2.run_phase -eq "FINISHED") `
            "V2 runtime did not reach FINISHED after exact terminalization"
        Assert-Condition (@($finalV2.drain_active_execution_ids).Count -eq 0) `
            "Drain retained an active execution after terminalization"
        Write-Host "DEMO DRAIN TERMINALIZATION: PASS"
    }
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
