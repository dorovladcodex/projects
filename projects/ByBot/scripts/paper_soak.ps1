[CmdletBinding()]
param(
    [double]$Hours = 24,
    [int]$SampleSeconds = 60,
    [int]$RestartAtPercent = 50,
    [string]$OutputDirectory,
    [int]$TransientFailureThresholdSeconds = 300,
    [switch]$ValidateHelpersOnly
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$StartedAt = [DateTimeOffset]::UtcNow
$Duration = [TimeSpan]::FromHours($Hours)
$FinishAt = $StartedAt.Add($Duration)
$RestartAt = $StartedAt.AddTicks(
    [long]($Duration.Ticks * ([decimal]$RestartAtPercent / [decimal]100))
)
$RunId = $StartedAt.ToString("yyyyMMddTHHmmssZ")
$ArtifactRoot = if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    Join-Path $ProjectRoot "artifacts\soak"
} else {
    [System.IO.Path]::GetFullPath($OutputDirectory)
}
$ArtifactDirectory = Join-Path $ArtifactRoot $RunId
$StatusPath = Join-Path $ArtifactDirectory "status.jsonl"
$TradesPath = Join-Path $ArtifactDirectory "trades.json"
$CandidatesPath = Join-Path $ArtifactDirectory "candidates.json"
$StdoutPath = Join-Path $ArtifactDirectory "uvicorn.stdout.log"
$StderrPath = Join-Path $ArtifactDirectory "uvicorn.stderr.log"
$SummaryPath = Join-Path $ArtifactDirectory "summary.json"
$ReportPath = Join-Path $ArtifactDirectory "report.md"

$UvicornProcess = $null
$UvicornGeneration = 0
$TestPort = $null
$BaseUrl = $null
$PostgresService = $null
$DbUser = $null
$DbName = $null
$DatabaseUrl = $null
$SavedEnvironment = @{}
$Warnings = New-Object System.Collections.Generic.List[string]
$Failures = New-Object System.Collections.Generic.List[string]
$Samples = 0
$RestartCount = 0
$PersistenceOutages = 0
$KillSwitchActivations = 0
$PreviousKillSwitch = $false
$MaximumDrawdown = [decimal]0
$MarketFailureSince = $null
$NewsFailureSince = $null
$PreviousCandidateStates = @{}
$InitialStatus = $null
$FinalStatus = $null
$FinalTrades = $null
$FinalCandidates = $null
$LastSnapshot = $null
$OverallPassed = $false

$SensitiveEnvironmentNames = @(
    "DATABASE_URL", "BYBIT_API_KEY", "BYBIT_API_SECRET", "LLM_API_KEY",
    "OPENAI_API_KEY", "CODEX_API_KEY", "TELEGRAM_BOT_TOKEN"
)

$ChildEnvironment = @{
    APP_ENV = "local"
    TEST_MODE = "false"
    BOT_MODE = "PAPER"
    AUTO_PAPER_EXECUTION = "true"
    BYBIT_ENABLE_TRADING = "false"
    NEWS_CLASSIFIER_MODE = "codex_cli"
    CODEX_CLI_ENABLED = "true"
    NEWS_ENABLE_RSS = "true"
    MARKET_DATA_PROVIDER = "BYBIT_REST"
}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

function Get-DecimalAbs {
    param([decimal]$Value)
    if ($Value -lt 0) { return -$Value }
    return $Value
}

function Get-RequiredProperty {
    param(
        [Parameter(Mandatory = $true)][object]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Source
    )
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        throw "Required field is unavailable: source=$Source field=$Name"
    }
    return $property.Value
}

function Get-RequiredDecimal {
    param(
        [Parameter(Mandatory = $true)][object]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Source
    )
    $value = Get-RequiredProperty -Object $Object -Name $Name -Source $Source
    try { $decimal = [decimal]$value }
    catch { throw "Invalid decimal field: source=$Source field=$Name" }
    if ($decimal -ne $decimal) {
        throw "NaN financial field: source=$Source field=$Name"
    }
    return $decimal
}

function Assert-DecimalNear {
    param(
        [Parameter(Mandatory = $true)][AllowNull()][object]$Actual,
        [Parameter(Mandatory = $true)][AllowNull()][object]$Expected,
        [decimal]$Tolerance = [decimal]0.000001,
        [string]$Source = "unknown",
        [string]$Field = "value"
    )
    if ($null -eq $Actual -or $null -eq $Expected) {
        throw "Decimal comparison received null: source=$Source field=$Field"
    }
    try {
        $actualDecimal = [decimal]$Actual
        $expectedDecimal = [decimal]$Expected
    }
    catch {
        throw "Decimal comparison received invalid data: source=$Source field=$Field"
    }
    $difference = Get-DecimalAbs ($actualDecimal - $expectedDecimal)
    if ($difference -gt $Tolerance) {
        throw (
            "Decimal mismatch: source=$Source field=$Field actual=$actualDecimal " +
            "expected=$expectedDecimal tolerance=$Tolerance difference=$difference"
        )
    }
}

function Protect-SensitiveText {
    param([AllowNull()][string]$Text)
    if ($null -eq $Text) { return "" }
    $safe = $Text
    foreach ($name in $SensitiveEnvironmentNames) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if ($value) { $safe = $safe.Replace($value, "***") }
    }
    $safe = [regex]::Replace(
        $safe,
        '(?i)(postgresql(?:\+psycopg)?://[^:\s/]+:)[^@\s/]+@',
        '$1***@'
    )
    return $safe
}

function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Argument)
    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') {
        return $Argument
    }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append([char]34)
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes++
            continue
        }
        if ($character -eq [char]34) {
            [void]$builder.Append([char]92, (($backslashes * 2) + 1))
            [void]$builder.Append([char]34)
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append([char]92, $backslashes)
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append([char]92, ($backslashes * 2))
    }
    [void]$builder.Append([char]34)
    return $builder.ToString()
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [switch]$Quiet,
        [switch]$Sensitive
    )
    $process = New-Object System.Diagnostics.Process
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (($Arguments | ForEach-Object {
        ConvertTo-NativeArgument ([string]$_)
    }) -join " ")
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw "Native command could not be started." }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = $stdoutTask.Result
        $stderr = $stderrTask.Result
        $exitCode = $process.ExitCode
    }
    finally {
        $process.Dispose()
    }
    $safeStdout = Protect-SensitiveText $stdout
    $safeStderr = Protect-SensitiveText $stderr
    if (-not $Quiet -and -not $Sensitive) {
        if ($safeStdout) { Write-Host $safeStdout.TrimEnd() }
        if ($safeStderr) { Write-Host $safeStderr.TrimEnd() }
    }
    if ($exitCode -ne 0) {
        $details = (($safeStdout, $safeStderr) -join "`n").Trim()
        $executableName = [System.IO.Path]::GetFileName($FilePath)
        $message = "Native command '$executableName' failed with exit code $exitCode."
        if ($details) { $message = "$message`n$details" }
        throw $message
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        StdOut = $stdout
        StdErr = $stderr
    }
}

function Invoke-Docker {
    param([string[]]$Arguments, [switch]$Quiet, [switch]$Sensitive)
    return Invoke-NativeCommand -FilePath "docker" -Arguments $Arguments `
        -Quiet:$Quiet -Sensitive:$Sensitive
}

function Get-AvailablePort {
    $listener = New-Object System.Net.Sockets.TcpListener(
        [System.Net.IPAddress]::Loopback, 0
    )
    $listener.Start()
    try { return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}

function Resolve-CodexExecutable {
    $candidates = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($env:CODEX_CLI_PATH)) {
        $candidates.Add($env:CODEX_CLI_PATH)
    }
    $command = Get-Command codex -ErrorAction SilentlyContinue
    if ($null -ne $command -and $command.Source) {
        $candidates.Add($command.Source)
    }
    $npmVendor = Join-Path $env:APPDATA (
        "npm\node_modules\@openai\codex\node_modules\@openai\" +
        "codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe"
    )
    if (Test-Path -LiteralPath $npmVendor) { $candidates.Add($npmVendor) }
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        try {
            Invoke-NativeCommand -FilePath $candidate -Arguments @("--version") `
                -Quiet -Sensitive | Out-Null
            return $candidate
        }
        catch { }
    }
    throw "A runnable authenticated Codex CLI executable is unavailable."
}

function Safe-ReadTextFile {
    param([string]$Path, [int]$TimeoutMilliseconds = 5000)
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    $delay = 50
    while ($true) {
        $stream = $null
        $reader = $null
        try {
            $stream = New-Object System.IO.FileStream(
                $Path,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::ReadWrite
            )
            $reader = New-Object System.IO.StreamReader($stream)
            return $reader.ReadToEnd()
        }
        catch [System.IO.IOException] {
            if ([DateTime]::UtcNow -ge $deadline) { throw }
            Start-Sleep -Milliseconds $delay
            $delay = [math]::Min($delay * 2, 800)
        }
        finally {
            if ($null -ne $reader) { $reader.Dispose() }
            elseif ($null -ne $stream) { $stream.Dispose() }
        }
    }
}

function Add-ArtifactText {
    param([string]$Path, [string]$Text)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::AppendAllText($Path, $Text, $encoding)
}

function Write-ArtifactJson {
    param([string]$Path, [object]$Value, [int]$Depth = 30)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $json = $Value | ConvertTo-Json -Depth $Depth
    [System.IO.File]::WriteAllText($Path, $json, $encoding)
}

function Invoke-Api {
    param([string]$Path, [int]$TimeoutSec = 20)
    try {
        return Invoke-RestMethod -Method GET -Uri "$BaseUrl$Path" `
            -TimeoutSec $TimeoutSec
    }
    catch {
        throw "API request failed: path=$Path error=$($_.Exception.Message)"
    }
}

function Wait-Api {
    param([int]$TimeoutSeconds = 90)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -ne $UvicornProcess -and $UvicornProcess.HasExited) {
            throw "FastAPI child process exited during startup."
        }
        try {
            $health = Invoke-Api "/health" 3
            if (@("ok", "healthy") -contains [string]$health.status) {
                return Invoke-Api "/status" 10
            }
        }
        catch { Start-Sleep -Milliseconds 500 }
    }
    throw "FastAPI did not become healthy before timeout."
}

function Get-PostgresService {
    $services = (Invoke-Docker @("compose", "config", "--services") -Quiet).StdOut `
        -split "`r?`n" | Where-Object { $_ }
    foreach ($service in $services) {
        $image = (Invoke-Docker @(
            "compose", "config", "--format", "json"
        ) -Quiet).StdOut | ConvertFrom-Json
        $serviceConfig = $image.services.PSObject.Properties[$service].Value
        if ($serviceConfig.image -match "postgres") { return $service }
    }
    throw "PostgreSQL compose service was not found."
}

function Get-ContainerEnvironmentValue {
    param([string]$Service, [string]$Name)
    $result = Invoke-Docker @(
        "compose", "exec", "-T", $Service,
        "sh", "-lc", ('printf %s "$' + $Name + '"')
    ) -Quiet -Sensitive
    $value = $result.StdOut.Trim()
    if (-not $value) { throw "PostgreSQL environment value is unavailable: $Name" }
    return $value
}

function Wait-PostgresHealthy {
    param([string]$Service, [int]$TimeoutSeconds = 90)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $containerId = (Invoke-Docker @("compose", "ps", "-q", $Service) -Quiet).StdOut.Trim()
        if ($containerId) {
            $state = (Invoke-Docker @(
                "inspect", "--format", "{{.State.Health.Status}}", $containerId
            ) -Quiet).StdOut.Trim()
            if ($state -eq "healthy") { return }
        }
        Start-Sleep -Seconds 2
    }
    throw "PostgreSQL did not become healthy before timeout."
}

function Get-DatabaseScalar {
    param([string]$Sql)
    $result = Invoke-Docker @(
        "compose", "exec", "-T", $PostgresService,
        "psql", "-U", $DbUser, "-d", $DbName, "-tAc", $Sql
    ) -Quiet -Sensitive
    return $result.StdOut.Trim()
}

function Test-DatabaseInvariants {
    $queries = @{
        orphan_executions = @"
SELECT count(*) FROM paper_executions e
LEFT JOIN paper_positions p ON p.id=e.position_id
WHERE e.state IN ('PAPER_OPENED','PAPER_CLOSED') AND p.id IS NULL
"@
        duplicate_executions = @"
SELECT count(*) FROM (
  SELECT candidate_id FROM paper_executions GROUP BY candidate_id HAVING count(*) > 1
) q
"@
        duplicate_candidate_positions = @"
SELECT count(*) FROM (
  SELECT candidate_id FROM paper_positions
  WHERE candidate_id IS NOT NULL GROUP BY candidate_id HAVING count(*) > 1
) q
"@
        duplicate_open_symbols = @"
SELECT count(*) FROM (
  SELECT symbol FROM paper_positions WHERE status='OPEN'
  GROUP BY symbol HAVING count(*) > 1
) q
"@
        duplicate_closes = @"
SELECT count(*) FROM (
  SELECT id FROM paper_trades GROUP BY id HAVING count(*) > 1
) q
"@
    }
    $result = [ordered]@{}
    foreach ($name in $queries.Keys) {
        $raw = Get-DatabaseScalar $queries[$name]
        $parsed = 0
        if (-not [int]::TryParse($raw, [ref]$parsed)) {
            throw "Database invariant query returned invalid data: $name"
        }
        $result[$name] = $parsed
    }
    if ($result.orphan_executions -gt 0) {
        $Warnings.Add("orphan execution records detected")
    }
    if ($result.duplicate_executions -gt 0) {
        throw "duplicate paper executions detected"
    }
    if ($result.duplicate_candidate_positions -gt 0) {
        throw "duplicate paper positions for a candidate detected"
    }
    if ($result.duplicate_open_symbols -gt 0) {
        throw "multiple open positions for one symbol detected"
    }
    if ($result.duplicate_closes -gt 0) {
        throw "duplicate paper position close detected"
    }
    return [pscustomobject]$result
}

function Test-ProcessLogs {
    $text = ""
    if (Test-Path -LiteralPath $StdoutPath) {
        $text += Safe-ReadTextFile $StdoutPath
    }
    if (Test-Path -LiteralPath $StderrPath) {
        $text += "`n" + (Safe-ReadTextFile $StderrPath)
    }
    if ($null -ne $UvicornProcess) {
        if ($UvicornProcess.SoakStdoutPath) {
            $text += "`n" + (Safe-ReadTextFile $UvicornProcess.SoakStdoutPath)
        }
        if ($UvicornProcess.SoakStderrPath) {
            $text += "`n" + (Safe-ReadTextFile $UvicornProcess.SoakStderrPath)
        }
    }
    if ($text -match '(?im)Traceback \(most recent call last\)|Unhandled exception|Exception in ASGI application') {
        throw "unhandled exception detected in FastAPI logs"
    }
    if ($text -match '(?im)\b(place_order|submit_order|create_order)\b|exchange order placement attempted|Bybit (demo|live) order') {
        throw "exchange order adapter call detected in FastAPI logs"
    }
}

function Start-LocalUvicorn {
    $script:UvicornGeneration++
    $stdoutSegment = Join-Path $ArtifactDirectory (
        "uvicorn.stdout.$UvicornGeneration.log"
    )
    $stderrSegment = Join-Path $ArtifactDirectory (
        "uvicorn.stderr.$UvicornGeneration.log"
    )
    $process = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
        "--port", [string]$TestPort
    ) -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutSegment -RedirectStandardError $stderrSegment
    $process | Add-Member -NotePropertyName SoakStdoutPath `
        -NotePropertyValue $stdoutSegment
    $process | Add-Member -NotePropertyName SoakStderrPath `
        -NotePropertyValue $stderrSegment
    return $process
}

function Stop-LocalUvicorn {
    param([AllowNull()][System.Diagnostics.Process]$Process)
    if ($null -eq $Process) { return }
    $stdoutSegment = $Process.SoakStdoutPath
    $stderrSegment = $Process.SoakStderrPath
    try {
        if (-not $Process.HasExited) {
            try { $Process.CloseMainWindow() | Out-Null } catch { }
            if (-not $Process.WaitForExit(3000)) {
                try { $Process.Kill() } catch { }
                $Process.WaitForExit()
            }
        } else { $Process.WaitForExit() }
    }
    finally {
        $Process.Dispose()
        Start-Sleep -Milliseconds 150
        try {
            $stdout = Safe-ReadTextFile $stdoutSegment
            $stderr = Safe-ReadTextFile $stderrSegment
            if ($stdout) { Add-ArtifactText $StdoutPath $stdout }
            if ($stderr) { Add-ArtifactText $StderrPath $stderr }
        }
        catch {
            $Warnings.Add("could not collect one child-process log segment")
        }
    }
}

function Test-Accounting {
    param([object]$Pnl)
    $starting = Get-RequiredDecimal $Pnl "starting_equity" "/paper/pnl"
    $equity = Get-RequiredDecimal $Pnl "equity" "/paper/pnl"
    $realized = Get-RequiredDecimal $Pnl "realized_pnl" "/paper/pnl"
    $unrealized = Get-RequiredDecimal $Pnl "unrealized_pnl" "/paper/pnl"
    $fees = Get-RequiredDecimal $Pnl "fees_paid" "/paper/pnl"
    $expected = $starting + $realized + $unrealized
    Assert-DecimalNear -Actual $equity -Expected $expected `
        -Tolerance ([decimal]0.000001) -Source "/paper/pnl" -Field "equity"
    return [pscustomobject]@{
        starting_equity = $starting
        equity = $equity
        realized_pnl = $realized
        unrealized_pnl = $unrealized
        fees_paid = $fees
    }
}

function Test-CandidateTransitions {
    param([object[]]$Candidates)
    $allowed = @{
        PENDING_CONFIRMATION = @("PENDING_CONFIRMATION", "READY", "BLOCKED", "EXPIRED")
        READY = @("READY", "EXECUTING_PAPER", "PAPER_OPENED", "EXECUTION_BLOCKED", "EXPIRED")
        EXECUTING_PAPER = @("EXECUTING_PAPER", "PAPER_OPENED", "EXECUTION_BLOCKED")
        PAPER_OPENED = @("PAPER_OPENED", "PAPER_CLOSED")
        PAPER_CLOSED = @("PAPER_CLOSED")
        BLOCKED = @("BLOCKED")
        EXPIRED = @("EXPIRED")
        EXECUTION_BLOCKED = @("EXECUTION_BLOCKED")
    }
    foreach ($candidate in @($Candidates)) {
        $id = [string]$candidate.id
        $state = [string]$candidate.state
        if ($PreviousCandidateStates.ContainsKey($id)) {
            $previous = [string]$PreviousCandidateStates[$id]
            if ($allowed.ContainsKey($previous) -and $allowed[$previous] -notcontains $state) {
                $Warnings.Add("unexpected candidate transition: $id $previous -> $state")
            }
        }
        $PreviousCandidateStates[$id] = $state
    }
}

function Get-StateCounts {
    param([object[]]$Candidates)
    $counts = [ordered]@{}
    foreach ($candidate in @($Candidates)) {
        $state = [string]$candidate.state
        if (-not $counts.Contains($state)) { $counts[$state] = 0 }
        $counts[$state]++
    }
    return [pscustomobject]$counts
}

function Test-TransientStatus {
    param([object]$Status, [DateTimeOffset]$Now)
    $marketOk = [string]$Status.market_data_status -eq "OK"
    foreach ($propertyName in "latest_btcusdt_snapshot", "latest_ethusdt_snapshot") {
        $property = $Status.PSObject.Properties[$propertyName]
        if ($null -eq $property -or $null -eq $property.Value) {
            $marketOk = $false
            continue
        }
        try {
            $snapshotTime = [DateTimeOffset]::Parse([string]$property.Value.timestamp)
            if (($Now - $snapshotTime).TotalSeconds -gt $TransientFailureThresholdSeconds) {
                $marketOk = $false
            }
        }
        catch { $marketOk = $false }
    }
    if (-not $marketOk) {
        if ($null -eq $script:MarketFailureSince) { $script:MarketFailureSince = $Now }
        if (($Now - $script:MarketFailureSince).TotalSeconds -gt $TransientFailureThresholdSeconds) {
            throw "public market data remained unavailable or stale beyond threshold"
        }
    } else { $script:MarketFailureSince = $null }

    $newsFailed = [string]$Status.news_status -eq "ERROR"
    if ($newsFailed) {
        if ($null -eq $script:NewsFailureSince) { $script:NewsFailureSince = $Now }
        if (($Now - $script:NewsFailureSince).TotalSeconds -gt $TransientFailureThresholdSeconds) {
            throw "RSS news ingestion remained unavailable beyond threshold"
        }
    } else { $script:NewsFailureSince = $null }
}

function Get-SoakSnapshot {
    $now = [DateTimeOffset]::UtcNow
    if ($UvicornProcess.HasExited) { throw "FastAPI child process crashed." }
    $status = Invoke-Api "/status"
    $pnl = Invoke-Api "/paper/pnl"
    $positions = Invoke-Api "/paper/positions"
    $trades = Invoke-Api "/paper/trades"
    $candidatesResponse = Invoke-Api "/signals/candidates"
    $accounting = Test-Accounting $pnl
    $invariants = Test-DatabaseInvariants
    Test-ProcessLogs
    $candidates = @($candidatesResponse.candidates)
    Test-CandidateTransitions $candidates
    Test-TransientStatus $status $now

    if ([string]$status.persistence_status -ne "OK") {
        $script:PersistenceOutages++
        throw "persistence outage detected"
    }
    if ($status.order_placement_blocked -ne $true) {
        throw "exchange order placement is no longer blocked"
    }
    if ($status.live_trading -ne $false -or $status.trading_enabled -eq $true) {
        throw "live or exchange trading became enabled"
    }
    if ($status.kill_switch_active -eq $true -and -not $PreviousKillSwitch) {
        $script:KillSwitchActivations++
    }
    $script:PreviousKillSwitch = [bool]$status.kill_switch_active
    $drawdown = Get-RequiredDecimal $status "current_drawdown_pct" "/status"
    if ($drawdown -gt $MaximumDrawdown) { $script:MaximumDrawdown = $drawdown }

    $slippage = [decimal]0
    foreach ($trade in @($trades.trades)) {
        $slippage += Get-RequiredDecimal $trade "slippage_paid" "/paper/trades"
    }
    $stateCounts = Get-StateCounts $candidates
    return [pscustomobject][ordered]@{
        timestamp = $now.ToString("o")
        process_health = "OK"
        persistence_status = $status.persistence_status
        market_data_status = $status.market_data_status
        news_status = $status.news_status
        rss_items_seen = $status.rss_items_seen
        rss_items_accepted = $status.rss_items_accepted
        news_duplicates_skipped = $status.news_duplicates_skipped
        news_skipped_before_codex_count = $status.news_skipped_before_codex_count
        items_classified_count = $status.items_classified_count
        classifications_trade_eligible = $status.classifications_trade_eligible
        codex_cli_calls_count = $status.codex_cli_calls_count
        codex_cli_cache_hits = $status.codex_cli_cache_hits
        codex_cli_total_tokens_today = $status.codex_cli_total_tokens_today
        estimated_input_tokens = $status.estimated_input_tokens
        estimated_output_tokens = $status.estimated_output_tokens
        candidate_counts = $stateCounts
        paper_execution_attempts = $status.paper_execution_attempts
        paper_positions_opened = $status.paper_positions_opened
        paper_positions_closed = $status.paper_positions_closed
        open_positions = @($positions.positions).Count
        realized_pnl = $accounting.realized_pnl
        unrealized_pnl = $accounting.unrealized_pnl
        equity = $accounting.equity
        fees = $accounting.fees_paid
        slippage = $slippage
        daily_pnl = Get-RequiredDecimal $status "daily_pnl" "/status"
        weekly_pnl = Get-RequiredDecimal $status "weekly_pnl" "/status"
        drawdown_pct = $drawdown
        kill_switch_active = $status.kill_switch_active
        kill_switch_reasons = @($status.kill_switch_reasons)
        paper_execution_duplicates_blocked = $status.paper_execution_duplicates_blocked
        last_execution_error = $status.last_execution_error
        database_invariants = $invariants
    }
}

function Invoke-ControlledRestart {
    $beforeStatus = Invoke-Api "/status"
    $beforePnl = Invoke-Api "/paper/pnl"
    $beforePositions = @((Invoke-Api "/paper/positions").positions | ForEach-Object {
        [string]$_.id
    })
    $beforeCandidates = @((Invoke-Api "/signals/candidates").candidates)
    $closedBefore = @($beforeCandidates | Where-Object { $_.state -eq "PAPER_CLOSED" })
    $executionCountBefore = [int](Get-DatabaseScalar "SELECT count(*) FROM paper_executions")

    Stop-LocalUvicorn $UvicornProcess
    $script:UvicornProcess = $null
    $script:UvicornProcess = Start-LocalUvicorn
    $afterStatus = Wait-Api
    Assert-True ($afterStatus.persistence_status -eq "OK") `
        "persistence did not recover after restart"
    $afterPnl = Invoke-Api "/paper/pnl"
    foreach ($field in "starting_equity", "realized_pnl", "fees_paid") {
        Assert-DecimalNear `
            -Actual (Get-RequiredDecimal $afterPnl $field "/paper/pnl after restart") `
            -Expected (Get-RequiredDecimal $beforePnl $field "/paper/pnl before restart") `
            -Source "controlled restart" -Field $field
    }
    $afterPositions = @((Invoke-Api "/paper/positions").positions | ForEach-Object {
        [string]$_.id
    })
    foreach ($positionId in $beforePositions) {
        Assert-True ($afterPositions -contains $positionId) `
            "open paper position was not restored: $positionId"
    }
    $afterCandidates = @((Invoke-Api "/signals/candidates").candidates)
    foreach ($closed in $closedBefore) {
        $restored = @($afterCandidates | Where-Object { $_.id -eq $closed.id })
        Assert-True ($restored.Count -eq 1 -and $restored[0].state -eq "PAPER_CLOSED") `
            "closed candidate was reopened after restart"
    }
    $executionCountAfter = [int](Get-DatabaseScalar "SELECT count(*) FROM paper_executions")
    Assert-True ($executionCountAfter -ge $executionCountBefore) `
        "paper execution rows disappeared after restart"
    Test-DatabaseInvariants | Out-Null
    $script:RestartCount++
    Write-Host "CONTROLLED RESTART: PASS" -ForegroundColor Green
}

function Get-RunTradeMetrics {
    param([object[]]$Trades)
    $runTrades = @($Trades | Where-Object {
        $_.closed_at -and [DateTimeOffset]::Parse([string]$_.closed_at) -ge $StartedAt
    })
    $gross = [decimal]0
    $fees = [decimal]0
    $slippage = [decimal]0
    $net = [decimal]0
    $wins = 0
    $losses = 0
    foreach ($trade in $runTrades) {
        $tradeGross = Get-RequiredDecimal $trade "gross_pnl" "final trades"
        $tradeFees = Get-RequiredDecimal $trade "fees_paid" "final trades"
        $tradeSlippage = Get-RequiredDecimal $trade "slippage_paid" "final trades"
        $tradeNet = Get-RequiredDecimal $trade "realized_pnl" "final trades"
        $gross += $tradeGross
        $fees += $tradeFees
        $slippage += $tradeSlippage
        $net += $tradeNet
        if ($tradeNet -gt 0) { $wins++ }
        elseif ($tradeNet -lt 0) { $losses++ }
    }
    return [pscustomobject]@{
        opened = @($Trades | Where-Object {
            [DateTimeOffset]::Parse([string]$_.opened_at) -ge $StartedAt
        }).Count
        closed = $runTrades.Count
        wins = $wins
        losses = $losses
        gross_pnl = $gross
        fees = $fees
        slippage = $slippage
        net_pnl = $net
    }
}

function New-ReportText {
    param([object]$Summary)
    $warningLines = if (@($Summary.warnings).Count -gt 0) {
        (@($Summary.warnings) | ForEach-Object { "- $_" }) -join "`n"
    } else { "- None" }
    $failureLines = if (@($Summary.failures).Count -gt 0) {
        (@($Summary.failures) | ForEach-Object { "- $_" }) -join "`n"
    } else { "- None" }
    return @"
# PAPER soak report

- Start (UTC): $($Summary.started_at)
- Finish (UTC): $($Summary.finished_at)
- Duration seconds: $($Summary.duration_seconds)
- Process restart count: $($Summary.process_restart_count)
- Persistence outages: $($Summary.persistence_outages)
- News seen / accepted / skipped / duplicates: $($Summary.news_seen) / $($Summary.news_accepted) / $($Summary.news_skipped) / $($Summary.news_duplicates)
- Classifications / cache hits: $($Summary.classifications) / $($Summary.codex_cache_hits)
- Codex calls / total tokens today: $($Summary.codex_calls) / $($Summary.codex_total_tokens_today)
- Candidates by final state: $($Summary.candidates_by_state_json)
- Trades opened / closed: $($Summary.trades_opened) / $($Summary.trades_closed)
- Wins / losses: $($Summary.wins) / $($Summary.losses)
- Gross PnL: $($Summary.gross_pnl)
- Fees: $($Summary.fees)
- Slippage: $($Summary.slippage)
- Net PnL: $($Summary.net_pnl)
- Final equity: $($Summary.final_equity)
- Maximum drawdown (%): $($Summary.maximum_drawdown_pct)
- Kill-switch activations: $($Summary.kill_switch_activations)
- Duplicate attempts blocked: $($Summary.duplicate_attempts_blocked)
- Exchange execution safety: $($Summary.exchange_execution_safety)

## Warnings

$warningLines

## Failures

$failureLines
"@
}

function Test-Helpers {
    Assert-True ((Get-DecimalAbs ([decimal]-2.5)) -eq [decimal]2.5) `
        "decimal absolute value failed"
    Assert-DecimalNear -Actual ([decimal]10000.0000005) `
        -Expected ([decimal]10000) -Tolerance ([decimal]0.000001) `
        -Source "helper-test" -Field "within_tolerance"
    $failed = $false
    try {
        Assert-DecimalNear -Actual ([decimal]1.01) -Expected ([decimal]1) `
            -Tolerance ([decimal]0.001) -Source "helper-test" -Field "outside"
    } catch { $failed = $true }
    Assert-True $failed "outside-tolerance decimal assertion did not fail"
    $accounting = Test-Accounting ([pscustomobject]@{
        starting_equity = [decimal]10000
        equity = [decimal]10004
        realized_pnl = [decimal]3
        unrealized_pnl = [decimal]1
        fees_paid = [decimal]0.5
    })
    Assert-True ($accounting.equity -eq [decimal]10004) `
        "accounting helper returned an invalid equity"
    $report = New-ReportText ([pscustomobject]@{
        started_at="start"; finished_at="finish"; duration_seconds=1
        process_restart_count=1; persistence_outages=0; news_seen=1
        news_accepted=1; news_skipped=0; news_duplicates=0; classifications=1
        codex_cache_hits=0; codex_calls=1; codex_total_tokens_today=100
        candidates_by_state_json='{"READY":1}'; trades_opened=1; trades_closed=1
        wins=1; losses=0; gross_pnl=2; fees=0.5; slippage=0.1; net_pnl=1.4
        final_equity=10001.4; maximum_drawdown_pct=0; kill_switch_activations=0
        duplicate_attempts_blocked=0; exchange_execution_safety="PASS"
        warnings=@(); failures=@()
    })
    Assert-True ($report -match "PAPER soak report" -and $report -match "Final equity") `
        "report helper output is incomplete"
}

function Restore-Environment {
    foreach ($name in $SavedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable(
            $name, $SavedEnvironment[$name], "Process"
        )
    }
}

if ($ValidateHelpersOnly) {
    Test-Helpers
    Write-Host "HELPERS: PASS" -ForegroundColor Green
    return
}

if ($Hours -le 0) { throw "Hours must be greater than zero." }
if ($SampleSeconds -lt 1) { throw "SampleSeconds must be at least one." }
if ($RestartAtPercent -lt 1 -or $RestartAtPercent -gt 99) {
    throw "RestartAtPercent must be between 1 and 99."
}
if ($TransientFailureThresholdSeconds -lt $SampleSeconds) {
    throw "TransientFailureThresholdSeconds must be at least SampleSeconds."
}

try {
    New-Item -ItemType Directory -Path $ArtifactDirectory -Force | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($StatusPath, "", $encoding)
    [System.IO.File]::WriteAllText($StdoutPath, "", $encoding)
    [System.IO.File]::WriteAllText($StderrPath, "", $encoding)
    Test-Helpers

    Assert-True (Test-Path -LiteralPath $Python) "local Python environment is missing"
    Assert-True ($null -ne (Get-Command docker -ErrorAction SilentlyContinue)) `
        "Docker CLI is unavailable"
    Invoke-Docker @("--version") -Quiet | Out-Null
    Invoke-Docker @("compose", "version") -Quiet | Out-Null
    $PostgresService = Get-PostgresService
    Invoke-Docker @("compose", "up", "-d", "--no-deps", $PostgresService) `
        -Quiet | Out-Null
    Wait-PostgresHealthy $PostgresService
    $DbUser = Get-ContainerEnvironmentValue $PostgresService "POSTGRES_USER"
    $DbPassword = Get-ContainerEnvironmentValue $PostgresService "POSTGRES_PASSWORD"
    $DbName = Get-ContainerEnvironmentValue $PostgresService "POSTGRES_DB"
    $portLine = (Invoke-Docker @(
        "compose", "port", $PostgresService, "5432"
    ) -Quiet).StdOut.Trim()
    Assert-True ($portLine -match ":(?<port>\d+)$") `
        "could not detect PostgreSQL host port"
    $encodedUser = [uri]::EscapeDataString($DbUser)
    $encodedPassword = [uri]::EscapeDataString($DbPassword)
    $DatabaseUrl = (
        "postgresql+psycopg://$encodedUser`:$encodedPassword" +
        "@127.0.0.1:$($Matches.port)/$DbName"
    )

    $codexExecutable = Resolve-CodexExecutable
    $environmentNames = @($ChildEnvironment.Keys) + @(
        "DATABASE_URL", "CODEX_CLI_PATH"
    )
    foreach ($name in $environmentNames) {
        $SavedEnvironment[$name] = [Environment]::GetEnvironmentVariable(
            $name, "Process"
        )
    }
    foreach ($entry in $ChildEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable(
            $entry.Key, $entry.Value, "Process"
        )
    }
    [Environment]::SetEnvironmentVariable("DATABASE_URL", $DatabaseUrl, "Process")
    [Environment]::SetEnvironmentVariable(
        "CODEX_CLI_PATH", $codexExecutable, "Process"
    )

    Invoke-NativeCommand -FilePath $Python -Arguments @(
        "-m", "alembic", "upgrade", "head"
    ) -Quiet -Sensitive | Out-Null
    $heads = (Invoke-NativeCommand -FilePath $Python -Arguments @(
        "-m", "alembic", "heads"
    ) -Quiet -Sensitive).StdOut
    $current = (Invoke-NativeCommand -FilePath $Python -Arguments @(
        "-m", "alembic", "current"
    ) -Quiet -Sensitive).StdOut
    $headRevision = ([regex]::Match($heads, '[0-9_]+')).Value
    Assert-True ($headRevision -and $current -match [regex]::Escape($headRevision)) `
        "Alembic database revision is not at head"

    $TestPort = Get-AvailablePort
    $BaseUrl = "http://127.0.0.1:$TestPort"
    $UvicornProcess = Start-LocalUvicorn
    $InitialStatus = Wait-Api
    $health = Invoke-Api "/health"
    Assert-True (@("ok", "healthy") -contains [string]$health.status) `
        "/health is not healthy"
    Assert-True ($InitialStatus.persistence_status -eq "OK") `
        "persistence_status is not OK"
    Assert-True ($InitialStatus.auto_paper_execution -eq $true) `
        "automatic paper execution is not enabled in child process"
    Assert-True ($InitialStatus.order_placement_blocked -eq $true) `
        "exchange order placement is not blocked"
    Assert-True ($InitialStatus.live_trading -eq $false) "live trading is enabled"
    Assert-True ($InitialStatus.mode -eq "PAPER") "child process is not in PAPER mode"
    Assert-True ($InitialStatus.news_classifier_mode -eq "codex_cli") `
        "Codex CLI classifier mode is not active"
    $classifierStatus = Invoke-Api "/news/classifier/status"
    Assert-True (
        $classifierStatus.status -eq "OK" -and
        $classifierStatus.configured -eq $true -and
        $classifierStatus.provider_available -eq $true
    ) "Codex CLI classifier is not available"
    Get-RequiredDecimal $InitialStatus "paper_equity" "/status" | Out-Null
    Test-DatabaseInvariants | Out-Null
    Write-Host "PREFLIGHT: PASS" -ForegroundColor Green

    $nextSample = [DateTimeOffset]::UtcNow
    $restartDone = $false
    while ([DateTimeOffset]::UtcNow -lt $FinishAt) {
        $now = [DateTimeOffset]::UtcNow
        if ($UvicornProcess.HasExited) { throw "FastAPI child process crashed." }
        if (-not $restartDone -and $now -ge $RestartAt) {
            Invoke-ControlledRestart
            $restartDone = $true
        }
        if ($now -ge $nextSample) {
            $LastSnapshot = Get-SoakSnapshot
            Add-ArtifactText $StatusPath (
                ($LastSnapshot | ConvertTo-Json -Depth 30 -Compress) + "`n"
            )
            $Samples++
            $elapsed = $now - $StartedAt
            Write-Host (
                "SOAK RUNNING: {0:hh\:mm\:ss}/{1:hh\:mm\:ss}" -f `
                $elapsed, $Duration
            )
            $nextSample = $now.AddSeconds($SampleSeconds)
        }
        $remainingMilliseconds = [math]::Min(
            500, [math]::Max(50, ($FinishAt - $now).TotalMilliseconds)
        )
        Start-Sleep -Milliseconds ([int]$remainingMilliseconds)
    }

    $LastSnapshot = Get-SoakSnapshot
    Add-ArtifactText $StatusPath (
        ($LastSnapshot | ConvertTo-Json -Depth 30 -Compress) + "`n"
    )
    $FinalStatus = Invoke-Api "/status"
    $FinalTrades = Invoke-Api "/paper/trades"
    $FinalCandidates = Invoke-Api "/signals/candidates"
    Write-ArtifactJson $TradesPath $FinalTrades
    Write-ArtifactJson $CandidatesPath $FinalCandidates
    $tradeMetrics = Get-RunTradeMetrics @($FinalTrades.trades)
    $candidateCounts = Get-StateCounts @($FinalCandidates.candidates)
    $initialSeen = [int]$InitialStatus.rss_items_seen
    $finalSeen = [int]$FinalStatus.rss_items_seen
    $initialAccepted = [int]$InitialStatus.rss_items_accepted
    $finalAccepted = [int]$FinalStatus.rss_items_accepted
    $initialClassified = [int]$InitialStatus.items_classified_count
    $finalClassified = [int]$FinalStatus.items_classified_count
    $summary = [pscustomobject][ordered]@{
        started_at = $StartedAt.ToString("o")
        finished_at = [DateTimeOffset]::UtcNow.ToString("o")
        duration_seconds = [math]::Round(
            ([DateTimeOffset]::UtcNow - $StartedAt).TotalSeconds, 3
        )
        samples = $Samples + 1
        process_restart_count = $RestartCount
        persistence_outages = $PersistenceOutages
        news_seen = [math]::Max(0, $finalSeen - $initialSeen)
        news_accepted = [math]::Max(0, $finalAccepted - $initialAccepted)
        news_skipped = [int]$FinalStatus.news_skipped_before_codex_count
        news_duplicates = [int]$FinalStatus.news_duplicates_skipped
        classifications = [math]::Max(0, $finalClassified - $initialClassified)
        codex_calls = [int]$FinalStatus.codex_cli_calls_count
        codex_cache_hits = [int]$FinalStatus.codex_cli_cache_hits
        codex_total_tokens_today = $FinalStatus.codex_cli_total_tokens_today
        candidates_by_state = $candidateCounts
        candidates_by_state_json = ($candidateCounts | ConvertTo-Json -Compress)
        trades_opened = $tradeMetrics.opened
        trades_closed = $tradeMetrics.closed
        wins = $tradeMetrics.wins
        losses = $tradeMetrics.losses
        gross_pnl = $tradeMetrics.gross_pnl
        fees = $tradeMetrics.fees
        slippage = $tradeMetrics.slippage
        net_pnl = $tradeMetrics.net_pnl
        final_equity = Get-RequiredDecimal $FinalStatus "paper_equity" "/status"
        maximum_drawdown_pct = $MaximumDrawdown
        kill_switch_activations = $KillSwitchActivations
        duplicate_attempts_blocked = [int]$FinalStatus.paper_execution_duplicates_blocked
        warnings = @($Warnings | Select-Object -Unique)
        failures = @($Failures | Select-Object -Unique)
        exchange_execution_safety = "PASS"
        overall = "PASS"
    }
    Write-ArtifactJson $SummaryPath $summary
    $report = New-ReportText $summary
    [System.IO.File]::WriteAllText($ReportPath, $report, $encoding)
    $OverallPassed = $true
}
catch {
    $message = Protect-SensitiveText $_.Exception.Message
    $Failures.Add($message)
}
finally {
    Stop-LocalUvicorn $UvicornProcess
    $UvicornProcess = $null
    Restore-Environment
    if (-not $OverallPassed -and (Test-Path -LiteralPath $ArtifactDirectory)) {
        try {
            $failureSummary = [pscustomobject][ordered]@{
                started_at = $StartedAt.ToString("o")
                finished_at = [DateTimeOffset]::UtcNow.ToString("o")
                duration_seconds = [math]::Round(
                    ([DateTimeOffset]::UtcNow - $StartedAt).TotalSeconds, 3
                )
                samples = $Samples
                process_restart_count = $RestartCount
                persistence_outages = $PersistenceOutages
                warnings = @($Warnings | Select-Object -Unique)
                failures = @($Failures | Select-Object -Unique)
                exchange_execution_safety = "FAIL"
                overall = "FAIL"
            }
            Write-ArtifactJson $SummaryPath $failureSummary
            $failureReport = @"
# PAPER soak report

- Start (UTC): $($failureSummary.started_at)
- Finish (UTC): $($failureSummary.finished_at)
- Duration seconds: $($failureSummary.duration_seconds)
- Overall: FAIL

## Warnings

$((@($failureSummary.warnings) | ForEach-Object { "- $_" }) -join "`n")

## Failures

$((@($failureSummary.failures) | ForEach-Object { "- $_" }) -join "`n")
"@
            [System.IO.File]::WriteAllText($ReportPath, $failureReport, $encoding)
        }
        catch { Write-Warning "Could not write complete failure artifacts." }
    }
}

if (-not $OverallPassed) {
    Write-Host "REPORT: $ReportPath"
    Write-Host "OVERALL: FAIL" -ForegroundColor Red
    exit 1
}

Write-Host "ACCOUNTING: PASS" -ForegroundColor Green
Write-Host "PERSISTENCE: PASS" -ForegroundColor Green
Write-Host "EXCHANGE EXECUTION BLOCKED: PASS" -ForegroundColor Green
Write-Host "REPORT: $ReportPath"
Write-Host "OVERALL: PASS" -ForegroundColor Green
exit 0
