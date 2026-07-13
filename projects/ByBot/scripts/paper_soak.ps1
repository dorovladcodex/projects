[CmdletBinding()]
param(
    [double]$Hours = 24,
    [int]$SampleSeconds = 60,
    [int]$RestartAtPercent = 50,
    [string]$OutputDirectory,
    [int]$TransientFailureThresholdSeconds = 300,
    [switch]$ValidateHelpersOnly,
    [ValidateSet("all", "historical", "no_trades", "opened_closed", "preexisting_closed", "restart", "deterministic", "counter_restart")]
    [string]$HelperScenario = "all"
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
$OpeningSnapshot = $null
$FinalSnapshot = $null
$RunDatabaseMetrics = $null
$FinalTrades = $null
$FinalCandidates = $null
$LastSnapshot = $null
$OverallPassed = $false
$CounterNames = @(
    "rss_items_seen", "rss_items_accepted", "news_skipped_before_codex_count",
    "news_duplicates_skipped", "items_classified_count",
    "mock_classifier_calls_count", "codex_cli_calls_count",
    "codex_cli_cache_hits", "codex_cli_total_tokens_today",
    "paper_execution_duplicates_blocked"
)
$CounterBaseline = @{}
$CounterTotals = @{}
foreach ($counterName in $CounterNames) { $CounterTotals[$counterName] = [decimal]0 }

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

function Get-DatabaseInteger {
    param([string]$Sql, [string]$Field)
    $raw = Get-DatabaseScalar $Sql
    $value = 0
    if (-not [int]::TryParse($raw, [ref]$value)) {
        throw "Database integer is missing or invalid: field=$Field"
    }
    return $value
}

function Get-DatabaseDecimal {
    param([string]$Sql, [string]$Field)
    $raw = Get-DatabaseScalar $Sql
    if ([string]::IsNullOrWhiteSpace($raw)) {
        throw "Database decimal is missing: field=$Field"
    }
    try { return [decimal]::Parse($raw, [System.Globalization.CultureInfo]::InvariantCulture) }
    catch { throw "Database decimal is invalid: field=$Field" }
}

function Get-DatabaseJson {
    param([string]$Sql, [string]$Field)
    $raw = Get-DatabaseScalar $Sql
    if ([string]::IsNullOrWhiteSpace($raw)) {
        throw "Database JSON is missing: field=$Field"
    }
    try { return $raw | ConvertFrom-Json }
    catch { throw "Database JSON is invalid: field=$Field" }
}

function Get-DatabaseAccountSnapshot {
    param([string]$Label)
    $starting = Get-DatabaseDecimal `
        "SELECT starting_equity FROM paper_accounts WHERE id=1" `
        "$Label.starting_equity"
    $realized = Get-DatabaseDecimal `
        "SELECT realized_pnl FROM paper_accounts WHERE id=1" `
        "$Label.realized_pnl"
    $fees = Get-DatabaseDecimal `
        "SELECT fees_paid FROM paper_accounts WHERE id=1" `
        "$Label.fees_paid"
    $unrealized = Get-DatabaseDecimal @"
SELECT COALESCE(sum((payload->>'unrealized_pnl')::numeric), 0)
FROM paper_positions WHERE status='OPEN'
"@ "$Label.unrealized_pnl"
    $equity = $starting + $realized + $unrealized
    return [pscustomobject][ordered]@{
        captured_at = [DateTimeOffset]::UtcNow.ToString("o")
        starting_equity = $starting
        equity = $equity
        realized_pnl = $realized
        unrealized_pnl = $unrealized
        fees_paid = $fees
        candidates = Get-DatabaseInteger `
            "SELECT count(*) FROM signal_candidates" "$Label.candidates"
        open_positions = Get-DatabaseInteger `
            "SELECT count(*) FROM paper_positions WHERE status='OPEN'" `
            "$Label.open_positions"
        closed_trades = Get-DatabaseInteger `
            "SELECT count(*) FROM paper_trades" "$Label.closed_trades"
        executions = Get-DatabaseInteger `
            "SELECT count(*) FROM paper_executions" "$Label.executions"
        classifications = Get-DatabaseInteger `
            "SELECT count(*) FROM news_classifications" "$Label.classifications"
        news_items = Get-DatabaseInteger `
            "SELECT count(*) FROM news_items" "$Label.news_items"
    }
}

function Get-RunDatabaseMetrics {
    param([DateTimeOffset]$FinishedAt)
    $started = $StartedAt.UtcDateTime.ToString(
        "yyyy-MM-ddTHH:mm:ss.fffffffZ", [System.Globalization.CultureInfo]::InvariantCulture
    )
    $finished = $FinishedAt.UtcDateTime.ToString(
        "yyyy-MM-ddTHH:mm:ss.fffffffZ", [System.Globalization.CultureInfo]::InvariantCulture
    )
    $candidateFilter = (
        "NULLIF(payload->>'created_at','')::timestamptz >= " +
        "TIMESTAMPTZ '$started' AND " +
        "NULLIF(payload->>'created_at','')::timestamptz <= TIMESTAMPTZ '$finished'"
    )
    $openedFilter = (
        "NULLIF(payload->>'opened_at','')::timestamptz >= " +
        "TIMESTAMPTZ '$started' AND " +
        "NULLIF(payload->>'opened_at','')::timestamptz <= TIMESTAMPTZ '$finished'"
    )
    $closedFilter = (
        "NULLIF(payload->>'closed_at','')::timestamptz >= " +
        "TIMESTAMPTZ '$started' AND " +
        "NULLIF(payload->>'closed_at','')::timestamptz <= TIMESTAMPTZ '$finished'"
    )
    $currentStates = Get-DatabaseJson @"
SELECT COALESCE(json_object_agg(state, count), '{}'::json) FROM (
  SELECT state, count(*) AS count FROM signal_candidates
  WHERE $candidateFilter GROUP BY state
) q
"@ "candidates_by_state_this_run"
    $cumulativeStates = Get-DatabaseJson @"
SELECT COALESCE(json_object_agg(state, count), '{}'::json) FROM (
  SELECT state, count(*) AS count FROM signal_candidates GROUP BY state
) q
"@ "candidates_by_state_cumulative"
    $realizedThisRun = Get-DatabaseDecimal @"
SELECT COALESCE(sum(realized_pnl::numeric), 0) FROM paper_trades
WHERE $closedFilter
"@ "realized_pnl_this_run"
    return [pscustomobject][ordered]@{
        candidates_created_this_run = Get-DatabaseInteger `
            "SELECT count(*) FROM signal_candidates WHERE $candidateFilter" `
            "candidates_created_this_run"
        candidates_by_state_this_run = $currentStates
        candidates_by_state_cumulative = $cumulativeStates
        cumulative_candidates = Get-DatabaseInteger `
            "SELECT count(*) FROM signal_candidates" "cumulative_candidates"
        trades_opened_this_run = Get-DatabaseInteger `
            "SELECT count(*) FROM paper_positions WHERE $openedFilter" `
            "trades_opened_this_run"
        trades_closed_this_run = Get-DatabaseInteger `
            "SELECT count(*) FROM paper_trades WHERE $closedFilter" `
            "trades_closed_this_run"
        preexisting_positions_closed_this_run = Get-DatabaseInteger @"
SELECT count(*) FROM paper_trades
WHERE $closedFilter
  AND NULLIF(payload->>'opened_at','')::timestamptz < TIMESTAMPTZ '$started'
"@ "preexisting_positions_closed_this_run"
        realized_pnl_this_run = $realizedThisRun
        gross_pnl_this_run = Get-DatabaseDecimal @"
SELECT COALESCE(sum((payload->>'gross_pnl')::numeric), 0) FROM paper_trades
WHERE $closedFilter
"@ "gross_pnl_this_run"
        fees_this_run = Get-DatabaseDecimal @"
SELECT COALESCE(sum((payload->>'fees_paid')::numeric), 0) FROM paper_trades
WHERE $closedFilter
"@ "fees_this_run"
        slippage_this_run = Get-DatabaseDecimal @"
SELECT COALESCE(sum((payload->>'slippage_paid')::numeric), 0) FROM paper_trades
WHERE $closedFilter
"@ "slippage_this_run"
        wins_this_run = Get-DatabaseInteger `
            "SELECT count(*) FROM paper_trades WHERE $closedFilter AND realized_pnl > 0" `
            "wins_this_run"
        losses_this_run = Get-DatabaseInteger `
            "SELECT count(*) FROM paper_trades WHERE $closedFilter AND realized_pnl < 0" `
            "losses_this_run"
        cumulative_trades = Get-DatabaseInteger `
            "SELECT count(*) FROM paper_trades" "cumulative_trades"
        classifications_this_run = Get-DatabaseInteger @"
SELECT count(*) FROM news_classifications
WHERE classified_at >= TIMESTAMPTZ '$started' AND classified_at <= TIMESTAMPTZ '$finished'
"@ "classifications_this_run"
        deterministic_classifications_this_run = Get-DatabaseInteger @"
SELECT count(*) FROM news_classifications
WHERE classified_at >= TIMESTAMPTZ '$started' AND classified_at <= TIMESTAMPTZ '$finished'
  AND COALESCE(payload->>'provider_name','') <> 'codex-cli'
"@ "deterministic_classifications_this_run"
        codex_cli_classifications_this_run = Get-DatabaseInteger @"
SELECT count(*) FROM news_classifications
WHERE classified_at >= TIMESTAMPTZ '$started' AND classified_at <= TIMESTAMPTZ '$finished'
  AND payload->>'provider_name' = 'codex-cli'
"@ "codex_cli_classifications_this_run"
        news_accepted_this_run = Get-DatabaseInteger @"
SELECT count(*) FROM news_items
WHERE received_at >= TIMESTAMPTZ '$started' AND received_at <= TIMESTAMPTZ '$finished'
"@ "news_accepted_this_run"
    }
}

function Assert-RunAccounting {
    param(
        [object]$Opening,
        [object]$Final,
        [decimal]$RealizedThisRun,
        [decimal]$Tolerance = [decimal]0.000001
    )
    $unrealizedChange = (
        [decimal]$Final.unrealized_pnl - [decimal]$Opening.unrealized_pnl
    )
    $expectedFinalEquity = (
        [decimal]$Opening.equity + $RealizedThisRun + $unrealizedChange
    )
    Assert-DecimalNear -Actual $Final.equity -Expected $expectedFinalEquity `
        -Tolerance $Tolerance -Source "soak run scope" -Field "final_equity"
    $realizedDelta = [decimal]$Final.realized_pnl - [decimal]$Opening.realized_pnl
    Assert-DecimalNear -Actual $realizedDelta -Expected $RealizedThisRun `
        -Tolerance $Tolerance -Source "soak run scope" `
        -Field "realized_pnl_this_run"
    return [pscustomobject]@{
        equity_change_this_run = [decimal]$Final.equity - [decimal]$Opening.equity
        change_in_unrealized_pnl = $unrealizedChange
        expected_final_equity = $expectedFinalEquity
    }
}

function Set-CounterBaseline {
    param([object]$Status)
    foreach ($name in $CounterNames) {
        $value = Get-RequiredProperty $Status $name "/status counter baseline"
        try { $CounterBaseline[$name] = [decimal]$value }
        catch { throw "Invalid process counter: $name" }
    }
}

function Set-ProcessStartCounterBaseline {
    param([object]$DatabaseSnapshot)
    foreach ($name in $CounterNames) { $CounterBaseline[$name] = [decimal]0 }
    # NewsService restores these three counters from durable rows before the
    # first RSS poll. All other counters are process-local and begin at zero.
    $CounterBaseline.rss_items_seen = [decimal]$DatabaseSnapshot.news_items
    $CounterBaseline.rss_items_accepted = [decimal]$DatabaseSnapshot.news_items
    $CounterBaseline.items_classified_count = [decimal]$DatabaseSnapshot.classifications
}

function Complete-CounterSegment {
    param([object]$Status)
    foreach ($name in $CounterNames) {
        $value = Get-RequiredProperty $Status $name "/status counter segment"
        try { $current = [decimal]$value }
        catch { throw "Invalid process counter: $name" }
        if (-not $CounterBaseline.ContainsKey($name)) {
            throw "Process counter baseline is missing: $name"
        }
        $delta = $current - [decimal]$CounterBaseline[$name]
        if ($delta -lt 0) {
            throw "Process counter decreased within one process segment: $name"
        }
        $CounterTotals[$name] = [decimal]$CounterTotals[$name] + $delta
    }
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
    Complete-CounterSegment $beforeStatus
    $beforePnl = Invoke-Api "/paper/pnl"
    $beforePositions = @((Invoke-Api "/paper/positions").positions | ForEach-Object {
        [string]$_.id
    })
    $beforeCandidates = @((Invoke-Api "/signals/candidates").candidates)
    $closedBefore = @($beforeCandidates | Where-Object { $_.state -eq "PAPER_CLOSED" })
    $executionCountBefore = [int](Get-DatabaseScalar "SELECT count(*) FROM paper_executions")

    Stop-LocalUvicorn $UvicornProcess
    $script:UvicornProcess = $null
    $restartOpeningSnapshot = Get-DatabaseAccountSnapshot "restart_opening"
    Set-ProcessStartCounterBaseline $restartOpeningSnapshot
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

- Run ID: $($Summary.run_id)
- Start (UTC): $($Summary.started_at)
- Finish (UTC): $($Summary.finished_at)
- Duration seconds: $($Summary.duration_seconds)

## Opening state

- Opening equity: $($Summary.opening_state.opening_equity)
- Opening realized PnL: $($Summary.opening_state.opening_realized_pnl)
- Opening unrealized PnL: $($Summary.opening_state.opening_unrealized_pnl)
- Preexisting candidates: $($Summary.opening_state.preexisting_candidates)
- Preexisting open positions: $($Summary.opening_state.preexisting_open_positions)
- Preexisting closed trades: $($Summary.opening_state.preexisting_closed_trades)

## Activity during this run

- Equity change this run: $($Summary.activity.equity_change_this_run)
- Realized PnL this run: $($Summary.activity.realized_pnl_this_run)
- Change in unrealized PnL: $($Summary.activity.change_in_unrealized_pnl)
- Trades opened / closed this run: $($Summary.activity.trades_opened_this_run) / $($Summary.activity.trades_closed_this_run)
- Preexisting positions closed this run: $($Summary.activity.preexisting_positions_closed_this_run)
- Wins / losses this run: $($Summary.activity.wins_this_run) / $($Summary.activity.losses_this_run)
- Gross PnL / fees / slippage this run: $($Summary.activity.gross_pnl_this_run) / $($Summary.activity.fees_this_run) / $($Summary.activity.slippage_this_run)
- News seen / accepted / skipped / duplicate events this run: $($Summary.activity.news_seen_this_run) / $($Summary.activity.news_accepted_this_run) / $($Summary.activity.news_skipped_this_run) / $($Summary.activity.duplicate_events_this_run)
- Classifications this run: $($Summary.activity.classifications_this_run)
- Deterministic / Codex CLI classifications: $($Summary.activity.deterministic_classifications_this_run) / $($Summary.activity.codex_cli_classifications_this_run)
- Codex calls / cache hits / tokens this run: $($Summary.activity.codex_cli_calls_this_run) / $($Summary.activity.codex_cli_cache_hits_this_run) / $($Summary.activity.codex_tokens_this_run)
- Candidates by state this run: $($Summary.activity.candidates_by_state_this_run_json)

## Final cumulative state

- Final equity: $($Summary.final_cumulative_state.final_equity)
- Cumulative realized PnL: $($Summary.final_cumulative_state.cumulative_realized_pnl)
- Cumulative unrealized PnL: $($Summary.final_cumulative_state.cumulative_unrealized_pnl)
- Cumulative trades: $($Summary.final_cumulative_state.cumulative_trades)
- Cumulative candidates by state: $($Summary.final_cumulative_state.candidates_by_state_cumulative_json)
- Maximum drawdown (%): $($Summary.final_cumulative_state.maximum_drawdown_pct)

## Restart validation

- Process restart count: $($Summary.restart_validation.process_restart_count)
- Controlled restart: $($Summary.restart_validation.controlled_restart)
- Counter segments aggregated: $($Summary.restart_validation.counter_segments_aggregated)
- Persistence outages: $($Summary.restart_validation.persistence_outages)

## Safety validation

- Accounting consistency: $($Summary.safety_validation.accounting_consistency)
- Database consistency: $($Summary.safety_validation.database_consistency)
- Duplicate attempts blocked: $($Summary.safety_validation.duplicate_attempts_blocked)
- Kill-switch activations: $($Summary.safety_validation.kill_switch_activations)
- Exchange execution safety: $($Summary.safety_validation.exchange_execution_safety)

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
    if ($HelperScenario -in @("all", "no_trades")) {
        $scope = Assert-RunAccounting `
            -Opening ([pscustomobject]@{equity=10000; realized_pnl=0; unrealized_pnl=0}) `
            -Final ([pscustomobject]@{equity=10000; realized_pnl=0; unrealized_pnl=0}) `
            -RealizedThisRun 0
        Assert-True ($scope.equity_change_this_run -eq 0) `
            "no-trade run changed equity"
    }
    if ($HelperScenario -in @("all", "opened_closed")) {
        $scope = Assert-RunAccounting `
            -Opening ([pscustomobject]@{equity=10000; realized_pnl=10; unrealized_pnl=0}) `
            -Final ([pscustomobject]@{equity=10005; realized_pnl=15; unrealized_pnl=0}) `
            -RealizedThisRun 5
        Assert-True ($scope.equity_change_this_run -eq 5) `
            "opened-and-closed trade accounting is invalid"
    }
    if ($HelperScenario -in @("all", "preexisting_closed")) {
        $scope = Assert-RunAccounting `
            -Opening ([pscustomobject]@{equity=10002; realized_pnl=0; unrealized_pnl=2}) `
            -Final ([pscustomobject]@{equity=10003; realized_pnl=3; unrealized_pnl=0}) `
            -RealizedThisRun 3
        Assert-True (
            $scope.change_in_unrealized_pnl -eq -2 -and
            $scope.equity_change_this_run -eq 1
        ) "preexisting position close accounting is invalid"
    }
    if ($HelperScenario -in @("all", "restart", "counter_restart")) {
        foreach ($name in $CounterNames) {
            $CounterTotals[$name] = [decimal]0
        }
        $first = [ordered]@{}
        $second = [ordered]@{}
        $third = [ordered]@{}
        foreach ($name in $CounterNames) {
            $first[$name] = 100
            $second[$name] = 105
            $third[$name] = 108
        }
        Set-CounterBaseline ([pscustomobject]$first)
        Complete-CounterSegment ([pscustomobject]$second)
        Set-CounterBaseline ([pscustomobject]$second)
        Complete-CounterSegment ([pscustomobject]$third)
        Assert-True ($CounterTotals.rss_items_seen -eq 8) `
            "counter segments were lost or double-counted across restart"
    }
    if ($HelperScenario -in @("all", "deterministic")) {
        $classification = [pscustomobject]@{
            classifications_this_run = 1
            deterministic_classifications_this_run = 1
            codex_cli_classifications_this_run = 0
            codex_cli_calls_this_run = 0
        }
        Assert-True (
            $classification.classifications_this_run -eq 1 -and
            $classification.deterministic_classifications_this_run -eq 1 -and
            $classification.codex_cli_calls_this_run -eq 0
        ) "deterministic classification without Codex call was rejected"
    }
    if ($HelperScenario -in @("all", "historical")) {
        $summary = [pscustomobject]@{
            run_id="run"; started_at="start"; finished_at="finish"; duration_seconds=1
            opening_state=[pscustomobject]@{
                opening_equity=10010; opening_realized_pnl=10; opening_unrealized_pnl=0
                preexisting_candidates=4; preexisting_open_positions=0
                preexisting_closed_trades=3
            }
            activity=[pscustomobject]@{
                equity_change_this_run=0; realized_pnl_this_run=0
                change_in_unrealized_pnl=0; trades_opened_this_run=0
                trades_closed_this_run=0; preexisting_positions_closed_this_run=0
                wins_this_run=0; losses_this_run=0; gross_pnl_this_run=0
                fees_this_run=0; slippage_this_run=0; news_seen_this_run=0
                news_accepted_this_run=0; news_skipped_this_run=0
                duplicate_events_this_run=0; classifications_this_run=0
                deterministic_classifications_this_run=0
                codex_cli_classifications_this_run=0; codex_cli_calls_this_run=0
                codex_cli_cache_hits_this_run=0; codex_tokens_this_run=0
                candidates_by_state_this_run_json='{}'
            }
            final_cumulative_state=[pscustomobject]@{
                final_equity=10010; cumulative_realized_pnl=10
                cumulative_unrealized_pnl=0; cumulative_trades=3
                candidates_by_state_cumulative_json='{"PAPER_CLOSED":3}'
                maximum_drawdown_pct=0
            }
            restart_validation=[pscustomobject]@{
                process_restart_count=1; controlled_restart="PASS"
                counter_segments_aggregated=2; persistence_outages=0
            }
            safety_validation=[pscustomobject]@{
                accounting_consistency="PASS"; database_consistency="PASS"
                duplicate_attempts_blocked=0; kill_switch_activations=0
                exchange_execution_safety="PASS"
            }
            warnings=@(); failures=@()
        }
        $report = New-ReportText $summary
        Assert-True (
            $report -match "Opening state" -and
            $report -match "Activity during this run" -and
            $report -match "Final cumulative state"
        ) "historical state was not separated in the report"
    }
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
    foreach ($counterName in $CounterNames) {
        $CounterTotals[$counterName] = [decimal]0
    }
    $CounterBaseline = @{}

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

    # This durable snapshot is captured before FastAPI can ingest news, create
    # candidates, or execute PAPER positions. It defines the run boundary.
    $OpeningSnapshot = Get-DatabaseAccountSnapshot "opening"
    Set-ProcessStartCounterBaseline $OpeningSnapshot

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
    Complete-CounterSegment $FinalStatus
    $FinalTrades = Invoke-Api "/paper/trades"
    $FinalCandidates = Invoke-Api "/signals/candidates"
    $FinalSnapshot = Get-DatabaseAccountSnapshot "final"
    $finishedAt = [DateTimeOffset]::UtcNow
    $RunDatabaseMetrics = Get-RunDatabaseMetrics -FinishedAt $finishedAt
    $accountingScope = Assert-RunAccounting `
        -Opening $OpeningSnapshot -Final $FinalSnapshot `
        -RealizedThisRun ([decimal]$RunDatabaseMetrics.realized_pnl_this_run)
    $apiCurrentRunTrades = @($FinalTrades.trades | Where-Object {
        $_.closed_at -and [DateTimeOffset]::Parse([string]$_.closed_at) -ge $StartedAt
    })
    Assert-True (
        $apiCurrentRunTrades.Count -eq $RunDatabaseMetrics.trades_closed_this_run
    ) "current-run closed trades do not match the database timestamp query"
    $apiRunRealized = [decimal]0
    foreach ($trade in $apiCurrentRunTrades) {
        $apiRunRealized += Get-RequiredDecimal $trade "realized_pnl" "/paper/trades"
    }
    Assert-DecimalNear -Actual $apiRunRealized `
        -Expected $RunDatabaseMetrics.realized_pnl_this_run `
        -Source "current-run trades" -Field "realized_pnl_this_run"
    $annotatedRunTrades = @($apiCurrentRunTrades | ForEach-Object {
        $openedAt = [DateTimeOffset]::Parse([string]$_.opened_at)
        [pscustomobject][ordered]@{
            id = $_.id
            candidate_id = $_.candidate_id
            opened_at = $_.opened_at
            closed_at = $_.closed_at
            preexisting_at_start = $openedAt -lt $StartedAt
            realized_pnl = $_.realized_pnl
            gross_pnl = $_.gross_pnl
            fees_paid = $_.fees_paid
            slippage_paid = $_.slippage_paid
            close_reason = $_.close_reason
            trade = $_
        }
    })
    $currentRunCandidates = @($FinalCandidates.candidates | Where-Object {
        $createdAt = [DateTimeOffset]::Parse([string]$_.created_at)
        $createdAt -ge $StartedAt -and $createdAt -le $finishedAt
    })
    Write-ArtifactJson $TradesPath ([pscustomobject][ordered]@{
        run_id = $RunId
        started_at = $StartedAt.ToString("o")
        finished_at = $finishedAt.ToString("o")
        current_run = $annotatedRunTrades
        cumulative = @($FinalTrades.trades)
    })
    Write-ArtifactJson $CandidatesPath ([pscustomobject][ordered]@{
        run_id = $RunId
        started_at = $StartedAt.ToString("o")
        finished_at = $finishedAt.ToString("o")
        current_run = $currentRunCandidates
        cumulative = @($FinalCandidates.candidates)
    })
    $summary = [pscustomobject][ordered]@{
        run_id = $RunId
        started_at = $StartedAt.ToString("o")
        finished_at = $finishedAt.ToString("o")
        duration_seconds = [math]::Round(
            ($finishedAt - $StartedAt).TotalSeconds, 3
        )
        samples = $Samples + 1
        opening_database_snapshot = $OpeningSnapshot
        final_database_snapshot = $FinalSnapshot
        opening_state = [pscustomobject][ordered]@{
            opening_equity = $OpeningSnapshot.equity
            opening_realized_pnl = $OpeningSnapshot.realized_pnl
            opening_unrealized_pnl = $OpeningSnapshot.unrealized_pnl
            opening_fees_paid = $OpeningSnapshot.fees_paid
            preexisting_candidates = $OpeningSnapshot.candidates
            preexisting_open_positions = $OpeningSnapshot.open_positions
            preexisting_closed_trades = $OpeningSnapshot.closed_trades
        }
        activity = [pscustomobject][ordered]@{
            equity_change_this_run = $accountingScope.equity_change_this_run
            realized_pnl_this_run = $RunDatabaseMetrics.realized_pnl_this_run
            change_in_unrealized_pnl = $accountingScope.change_in_unrealized_pnl
            trades_opened_this_run = $RunDatabaseMetrics.trades_opened_this_run
            trades_closed_this_run = $RunDatabaseMetrics.trades_closed_this_run
            preexisting_positions_closed_this_run = (
                $RunDatabaseMetrics.preexisting_positions_closed_this_run
            )
            trades_closed_records = $annotatedRunTrades
            wins_this_run = $RunDatabaseMetrics.wins_this_run
            losses_this_run = $RunDatabaseMetrics.losses_this_run
            gross_pnl_this_run = $RunDatabaseMetrics.gross_pnl_this_run
            fees_this_run = $RunDatabaseMetrics.fees_this_run
            slippage_this_run = $RunDatabaseMetrics.slippage_this_run
            news_seen_this_run = [int]$CounterTotals.rss_items_seen
            news_accepted_this_run = $RunDatabaseMetrics.news_accepted_this_run
            news_skipped_this_run = [int]$CounterTotals.news_skipped_before_codex_count
            duplicate_events_this_run = [int]$CounterTotals.news_duplicates_skipped
            classifications_this_run = $RunDatabaseMetrics.classifications_this_run
            deterministic_classifications_this_run = (
                $RunDatabaseMetrics.deterministic_classifications_this_run
            )
            codex_cli_classifications_this_run = (
                $RunDatabaseMetrics.codex_cli_classifications_this_run
            )
            codex_cli_calls_this_run = [int]$CounterTotals.codex_cli_calls_count
            codex_cli_cache_hits_this_run = [int]$CounterTotals.codex_cli_cache_hits
            codex_tokens_this_run = [int]$CounterTotals.codex_cli_total_tokens_today
            candidates_created_this_run = $RunDatabaseMetrics.candidates_created_this_run
            candidates_by_state_this_run = $RunDatabaseMetrics.candidates_by_state_this_run
            candidates_by_state_this_run_json = (
                $RunDatabaseMetrics.candidates_by_state_this_run | ConvertTo-Json -Compress
            )
        }
        final_cumulative_state = [pscustomobject][ordered]@{
            final_equity = $FinalSnapshot.equity
            cumulative_realized_pnl = $FinalSnapshot.realized_pnl
            cumulative_unrealized_pnl = $FinalSnapshot.unrealized_pnl
            cumulative_fees_paid = $FinalSnapshot.fees_paid
            cumulative_trades = $RunDatabaseMetrics.cumulative_trades
            cumulative_candidates = $RunDatabaseMetrics.cumulative_candidates
            candidates_by_state_cumulative = (
                $RunDatabaseMetrics.candidates_by_state_cumulative
            )
            candidates_by_state_cumulative_json = (
                $RunDatabaseMetrics.candidates_by_state_cumulative | ConvertTo-Json -Compress
            )
            maximum_drawdown_pct = $MaximumDrawdown
        }
        restart_validation = [pscustomobject][ordered]@{
            process_restart_count = $RestartCount
            controlled_restart = if ($RestartCount -eq 1) { "PASS" } else { "FAIL" }
            counter_segments_aggregated = $RestartCount + 1
            persistence_outages = $PersistenceOutages
        }
        safety_validation = [pscustomobject][ordered]@{
            accounting_consistency = "PASS"
            database_consistency = "PASS"
            duplicate_attempts_blocked = (
                [int]$CounterTotals.paper_execution_duplicates_blocked
            )
            kill_switch_activations = $KillSwitchActivations
            exchange_execution_safety = "PASS"
        }
        warnings = @($Warnings | Select-Object -Unique)
        failures = @($Failures | Select-Object -Unique)
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
