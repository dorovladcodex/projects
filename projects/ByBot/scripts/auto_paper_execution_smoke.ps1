[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$UvicornProcess = $null
$Failure = $null
$LastApiMethod = $null
$LastApiPath = $null
$LastApiStatus = $null
$LastApiResponse = $null
$CandidateId = $null
$PositionId = $null
$PostgresService = $null
$DbUser = $null
$SmokeDatabase = $null
$DatabaseCreated = $false
$LogDirectory = Join-Path ([System.IO.Path]::GetTempPath()) `
    ("bybot-auto-paper-smoke-" + [guid]::NewGuid().ToString("N"))
$StdoutLog = Join-Path $LogDirectory "uvicorn.stdout.log"
$StderrLog = Join-Path $LogDirectory "uvicorn.stderr.log"
$SavedEnvironment = @{}
$SensitiveEnvironmentNames = @(
    "DATABASE_URL", "BYBIT_API_KEY", "BYBIT_API_SECRET", "LLM_API_KEY",
    "OPENAI_API_KEY", "CODEX_API_KEY", "TELEGRAM_BOT_TOKEN"
)

$SafetyEnvironment = @{
    APP_ENV = "local"
    TEST_MODE = "true"
    BOT_MODE = "PAPER"
    AUTO_PAPER_EXECUTION = "true"
    BYBIT_ENABLE_TRADING = "false"
    NEWS_CLASSIFIER_MODE = "mock"
    NEWS_ENABLE_RSS = "false"
    NEWS_POLL_INTERVAL_SECONDS = "3600"
    SIGNAL_REEVALUATION_INTERVAL_SECONDS = "60"
    MARKET_DATA_PROVIDER = "MOCK"
    PAPER_STARTING_EQUITY_USDT = "10000"
    MAX_POSITION_NOTIONAL_PCT_OF_EQUITY = "5"
    MAX_POSITION_NOTIONAL_USDT = "5000"
}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

function Get-DecimalAbs {
    param([decimal]$Value)

    if ($Value -lt 0) {
        return -$Value
    }

    return $Value
}

function Assert-DecimalNear {
    param(
        [AllowNull()][object]$Actual,
        [AllowNull()][object]$Expected,
        [decimal]$Tolerance,
        [Parameter(Mandatory = $true)][string]$Endpoint,
        [Parameter(Mandatory = $true)][string]$Field
    )

    if ($null -eq $Actual -or $null -eq $Expected) {
        $actualText = if ($null -eq $Actual) { "<null>" } else { [string]$Actual }
        $expectedText = if ($null -eq $Expected) { "<null>" } else { [string]$Expected }
        throw "Decimal assertion failed: endpoint=$Endpoint field=$Field actual=$actualText expected=$expectedText tolerance=$Tolerance difference=<unavailable>"
    }
    try {
        $actualDecimal = [decimal]$Actual
        $expectedDecimal = [decimal]$Expected
    }
    catch {
        throw "Decimal assertion failed: endpoint=$Endpoint field=$Field actual=$Actual expected=$Expected tolerance=$Tolerance difference=<invalid decimal>"
    }
    $difference = Get-DecimalAbs ([decimal]$actualDecimal - [decimal]$expectedDecimal)
    if ($difference -gt $Tolerance) {
        throw "Decimal assertion failed: endpoint=$Endpoint field=$Field actual=$actualDecimal expected=$expectedDecimal tolerance=$Tolerance difference=$difference"
    }
}

function Test-DecimalHelpers {
    Assert-True ((Get-DecimalAbs ([decimal]2.5)) -eq [decimal]2.5) `
        "decimal positive difference helper failed"
    Assert-True ((Get-DecimalAbs ([decimal]-2.5)) -eq [decimal]2.5) `
        "decimal negative difference helper failed"
    Assert-DecimalNear -Actual ([decimal]1.25) -Expected ([decimal]1.25) `
        -Tolerance ([decimal]0.000001) -Endpoint "internal" -Field "exact_equality"
    Assert-DecimalNear -Actual ([decimal]1.0000005) -Expected ([decimal]1.0) `
        -Tolerance ([decimal]0.000001) -Endpoint "internal" -Field "within_tolerance"

    $outsideToleranceFailed = $false
    try {
        Assert-DecimalNear -Actual ([decimal]1.000002) -Expected ([decimal]1.0) `
            -Tolerance ([decimal]0.000001) -Endpoint "internal" -Field "outside_tolerance"
    }
    catch {
        $outsideToleranceFailed = $_.Exception.Message -match "difference="
    }
    Assert-True $outsideToleranceFailed "decimal outside-tolerance test did not fail"

    $nullFailed = $false
    try {
        Assert-DecimalNear -Actual $null -Expected ([decimal]1.0) `
            -Tolerance ([decimal]0.000001) -Endpoint "internal" -Field "null_value"
    }
    catch {
        $nullFailed = $_.Exception.Message -match "actual=<null>"
    }
    Assert-True $nullFailed "decimal null test did not fail"
}

function Get-RequiredDecimalProperty {
    param(
        [Parameter(Mandatory = $true)][object]$Response,
        [Parameter(Mandatory = $true)][string]$PropertyName,
        [Parameter(Mandatory = $true)][string]$SourceEndpoint
    )
    $property = $Response.PSObject.Properties[$PropertyName]
    if ($null -eq $property -or $null -eq $property.Value) {
        throw "Canonical accounting field is unavailable: endpoint=$SourceEndpoint field=$PropertyName"
    }
    try { return [decimal]$property.Value }
    catch {
        throw "Canonical accounting field is invalid: endpoint=$SourceEndpoint field=$PropertyName"
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
    $safe = [regex]::Replace(
        $safe,
        '(?im)\b(DATABASE_URL|BYBIT_API_KEY|BYBIT_API_SECRET|LLM_API_KEY|OPENAI_API_KEY|CODEX_API_KEY|TELEGRAM_BOT_TOKEN)\b\s*[:=]\s*[^\r\n]+',
        '$1=***'
    )
    return $safe
}

function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Argument)

    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') {
        return $Argument
    }

    # Apply the CommandLineToArgvW escaping rules used by Windows native
    # processes. PowerShell 5.1 does not expose ProcessStartInfo.ArgumentList.
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
        [switch]$Sensitive,
        [string[]]$RedactValues = @()
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
        if (-not $process.Start()) {
            throw "Native command '$FilePath' could not be started."
        }
        # Read both pipes concurrently to avoid a full stderr pipe blocking a
        # native command. This also avoids PowerShell 5.1 NativeCommandError.
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

    $safeStdout = $stdout
    $safeStderr = $stderr
    foreach ($value in $RedactValues) {
        if ($value) {
            $safeStdout = $safeStdout.Replace($value, "***")
            $safeStderr = $safeStderr.Replace($value, "***")
        }
    }
    $safeStdout = Protect-SensitiveText $safeStdout
    $safeStderr = Protect-SensitiveText $safeStderr
    if (-not $Quiet -and -not $Sensitive) {
        if ($safeStdout) { Write-Host $safeStdout.TrimEnd() }
        if ($safeStderr) { Write-Host $safeStderr.TrimEnd() }
    }
    if ($exitCode -ne 0) {
        $message = "Native command '$FilePath' failed with exit code $exitCode."
        if (-not $Sensitive) {
            $details = (($safeStdout, $safeStderr) -join "`n").Trim()
            if ($details) { $message = "$message`n$details" }
        }
        throw $message
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        StdOut = $stdout
        StdErr = $stderr
    }
}

function Safe-ReadTextFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$TimeoutMilliseconds = 5000
    )

    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    $delayMilliseconds = 50
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
            Start-Sleep -Milliseconds $delayMilliseconds
            $delayMilliseconds = [math]::Min($delayMilliseconds * 2, 800)
        }
        finally {
            if ($null -ne $reader) { $reader.Dispose() }
            elseif ($null -ne $stream) { $stream.Dispose() }
        }
    }
}

function Invoke-Docker {
    param(
        [string[]]$Arguments,
        [switch]$Quiet,
        [switch]$Sensitive
    )
    if ($Arguments.Count -gt 0 -and $Arguments[0] -eq "compose") {
        $tail = if ($Arguments.Count -gt 1) {
            @($Arguments[1..($Arguments.Count - 1)])
        } else { @() }
        $Arguments = @("compose", "--project-directory", $ProjectRoot) + $tail
    }
    return Invoke-NativeCommand -FilePath "docker" -Arguments $Arguments `
        -Quiet:$Quiet -Sensitive:$Sensitive
}

function Get-AvailablePort {
    $listener = New-Object System.Net.Sockets.TcpListener(
        [System.Net.IPAddress]::Loopback,
        0
    )
    try {
        $listener.Start()
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

function Invoke-Api {
    param(
        [ValidateSet("GET", "POST")][string]$Method,
        [string]$Path,
        [object]$Body = $null
    )

    $script:LastApiMethod = $Method
    $script:LastApiPath = $Path
    $script:LastApiStatus = $null
    $script:LastApiResponse = $null
    $parameters = @{
        Method = $Method
        Uri = "$script:BaseUrl$Path"
        TimeoutSec = 180
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json"
        $parameters.Body = $Body | ConvertTo-Json -Depth 30 -Compress
    }
    try {
        $response = Invoke-RestMethod @parameters
        $script:LastApiStatus = 200
        $script:LastApiResponse = $response | ConvertTo-Json -Depth 40
        return $response
    }
    catch {
        $webResponse = $_.Exception.Response
        if ($null -ne $webResponse) {
            try { $script:LastApiStatus = [int]$webResponse.StatusCode } catch { }
            try {
                $stream = $webResponse.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream)
                $script:LastApiResponse = $reader.ReadToEnd()
                $reader.Dispose()
            } catch { }
        }
        throw
    }
}

function Wait-Api {
    param([int]$TimeoutSeconds = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $status = Invoke-Api GET "/status"
            if ($null -ne $status) { return $status }
        }
        catch { Start-Sleep -Milliseconds 500 }
    }
    throw "FastAPI did not become ready within $TimeoutSeconds seconds."
}

function Wait-CandidateState {
    param(
        [string]$Id,
        [string]$ExpectedState,
        [int]$TimeoutSeconds = 30
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $last = $null
    while ((Get-Date) -lt $deadline) {
        $last = Invoke-Api GET "/signals/$Id"
        if ($last.candidate.state -eq $ExpectedState) { return $last }
        Start-Sleep -Milliseconds 500
    }
    $actual = if ($null -ne $last) { $last.candidate.state } else { "unavailable" }
    throw "Candidate did not reach $ExpectedState within $TimeoutSeconds seconds (actual=$actual)."
}

function Set-SmokeEnvironment {
    foreach ($entry in $SafetyEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
    }
    [Environment]::SetEnvironmentVariable("DATABASE_URL", $DatabaseUrl, "Process")
}

function Restore-Environment {
    foreach ($name in $SavedEnvironment.Keys) {
        try {
            [Environment]::SetEnvironmentVariable(
                $name,
                $SavedEnvironment[$name],
                "Process"
            )
        }
        catch {
            Write-Warning "Could not restore environment variable $name."
        }
    }
}

function Start-LocalUvicorn {
    Assert-True (Test-Path -LiteralPath $Python) "virtual environment Python is missing"
    $arguments = @(
        "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
        "--port", [string]$script:TestPort
    )
    Set-SmokeEnvironment
    try {
        # Start-Process inherits a snapshot of this process environment. Restore
        # the parent immediately so AUTO_PAPER_EXECUTION=true exists only in
        # the isolated FastAPI child after it starts.
        return Start-Process -FilePath $Python -ArgumentList $arguments `
            -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog
    }
    finally {
        Restore-Environment
    }
}

function Stop-LocalUvicorn {
    param($Process)
    if ($null -eq $Process) { return }

    try {
        $hasExited = $Process.HasExited
        if (-not $hasExited) {
            # Hidden uvicorn normally has no main window, but attempt a graceful
            # close first before falling back to termination.
            try { [void]$Process.CloseMainWindow() } catch { }
            try { $hasExited = $Process.WaitForExit(3000) } catch { $hasExited = $false }
        }
        if (-not $hasExited) {
            try { Stop-Process -Id $Process.Id -Force -ErrorAction Stop } catch {
                Write-Warning "Could not terminate uvicorn cleanly: $($_.Exception.Message)"
            }
        }
        try {
            if ($Process.WaitForExit(5000)) {
                # Parameterless WaitForExit flushes asynchronous process and
                # redirected-stream bookkeeping on .NET Framework.
                $Process.WaitForExit()
            } else {
                Write-Warning "Uvicorn did not exit within the cleanup timeout."
            }
        } catch {
            Write-Warning "Could not wait for uvicorn exit: $($_.Exception.Message)"
        }
    }
    catch {
        Write-Warning "Uvicorn cleanup warning: $($_.Exception.Message)"
    }
    finally {
        try { $Process.Dispose() } catch { }
        # Windows may release redirected file handles shortly after process exit.
        Start-Sleep -Milliseconds 250
    }
}

function Get-PostgresService {
    $result = Invoke-Docker @("compose", "config", "--format", "json") -Quiet
    $config = $result.StdOut | ConvertFrom-Json
    foreach ($property in $config.services.PSObject.Properties) {
        $service = $property.Value
        $image = [string]$service.image
        $health = ($service.healthcheck.test -join " ")
        if ($image -match "(^|/)postgres(:|$)" -or $health -match "pg_isready") {
            return $property.Name
        }
    }
    throw "Could not detect the PostgreSQL service from docker compose config."
}

function Wait-PostgresHealthy {
    param([string]$Service, [int]$TimeoutSeconds = 120)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $containerId = (Invoke-Docker @("compose", "ps", "-q", $Service) -Quiet).StdOut.Trim()
        if ($containerId) {
            $health = (Invoke-Docker @(
                "inspect", "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                $containerId
            ) -Quiet).StdOut.Trim()
            if ($health -eq "healthy") { return }
            if ($health -eq "unhealthy" -or $health -eq "exited") {
                throw "PostgreSQL container entered state '$health'."
            }
        }
        Start-Sleep -Seconds 2
    }
    throw "PostgreSQL did not become healthy within $TimeoutSeconds seconds."
}

function Get-ContainerEnvironmentValue {
    param([string]$Service, [string]$Name)
    $result = Invoke-Docker @("compose", "exec", "-T", $Service, "printenv", $Name) `
        -Quiet -Sensitive
    $value = $result.StdOut.Trim()
    Assert-True ([bool]$value) "PostgreSQL environment variable $Name is unavailable"
    return $value
}

function Get-PersistedCount {
    param([string]$Sql)
    $raw = (Invoke-Docker @(
        "compose", "exec", "-T", $PostgresService,
        "psql", "-U", $DbUser, "-d", $SmokeDatabase, "-tAc", $Sql
    ) -Quiet).StdOut.Trim()
    $count = 0
    Assert-True ([int]::TryParse($raw, [ref]$count)) `
        "PostgreSQL count query returned a non-numeric result"
    return $count
}

function Write-FailureDiagnostics {
    Write-Host "--- FAILURE DIAGNOSTICS ---" -ForegroundColor Yellow
    Write-Host "candidate_id: $CandidateId"
    Write-Host "position_id: $PositionId"
    Write-Host "last_api_method: $LastApiMethod"
    Write-Host "last_api_path: $LastApiPath"
    Write-Host "last_http_status: $LastApiStatus"
    Write-Host "last_api_response:"
    if ($LastApiResponse) {
        Write-Host (Protect-SensitiveText $LastApiResponse)
    } else { Write-Host "<none>" }
    if ($CandidateId) {
        try {
            $candidate = Invoke-Api GET "/signals/$CandidateId"
            Write-Host "candidate_response:"
            Write-Host ($candidate | ConvertTo-Json -Depth 40)
        } catch { }
        try {
            $positions = Invoke-Api GET "/paper/positions"
            $trades = Invoke-Api GET "/paper/trades"
            $pnl = Invoke-Api GET "/paper/pnl"
            Write-Host "paper_positions:"
            Write-Host ($positions | ConvertTo-Json -Depth 30)
            Write-Host "paper_trades:"
            Write-Host ($trades | ConvertTo-Json -Depth 30)
            Write-Host "paper_pnl:"
            Write-Host ($pnl | ConvertTo-Json -Depth 30)
        } catch { }
    }

    # Stop and dispose the process before touching redirected logs. Diagnostic
    # failures are warnings and must never replace the original assertion.
    Stop-LocalUvicorn $script:UvicornProcess
    $script:UvicornProcess = $null
    try {
        $serverOutput = Safe-ReadTextFile $StdoutLog
        $serverError = Safe-ReadTextFile $StderrLog
        foreach ($secret in @($DbPassword, $EncodedPassword, $DatabaseUrl)) {
            if ($secret) {
                $serverOutput = $serverOutput.Replace($secret, "***")
                $serverError = $serverError.Replace($secret, "***")
            }
        }
        $serverOutput = Protect-SensitiveText $serverOutput
        $serverError = Protect-SensitiveText $serverError
        if ($serverOutput) {
            Write-Host "uvicorn_stdout:"
            Write-Host $serverOutput.TrimEnd()
        }
        if ($serverError) {
            Write-Host "uvicorn_stderr:"
            Write-Host $serverError.TrimEnd()
        }
    }
    catch {
        Write-Warning "Could not read uvicorn diagnostic logs: $($_.Exception.Message)"
    }
}

try {
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
    Test-DecimalHelpers
    Assert-True ($null -ne (Get-Command docker -ErrorAction SilentlyContinue)) `
        "Docker CLI is unavailable"
    Assert-True (Test-Path -LiteralPath $Python) `
        "run the Windows local setup before this smoke test"
    Invoke-Docker @("--version") -Quiet | Out-Null
    Invoke-Docker @("compose", "version") -Quiet | Out-Null

    $PostgresService = Get-PostgresService
    Invoke-Docker @("compose", "up", "-d", "--no-deps", $PostgresService) | Out-Null
    Wait-PostgresHealthy -Service $PostgresService

    $DbUser = Get-ContainerEnvironmentValue $PostgresService "POSTGRES_USER"
    $DbPassword = Get-ContainerEnvironmentValue $PostgresService "POSTGRES_PASSWORD"
    $PortLine = (Invoke-Docker @(
        "compose", "port", $PostgresService, "5432"
    ) -Quiet).StdOut.Trim()
    Assert-True ($PortLine -match ":(?<port>\d+)$") `
        "could not detect the published PostgreSQL port"
    $DbPort = $Matches.port
    $SmokeDatabase = "bybot_auto_smoke_" + [guid]::NewGuid().ToString("N")
    Invoke-Docker @(
        "compose", "exec", "-T", $PostgresService,
        "createdb", "-U", $DbUser, $SmokeDatabase
    ) -Quiet | Out-Null
    $DatabaseCreated = $true

    $EncodedUser = [uri]::EscapeDataString($DbUser)
    $EncodedPassword = [uri]::EscapeDataString($DbPassword)
    $DatabaseUrl = "postgresql+psycopg://$EncodedUser`:$EncodedPassword@127.0.0.1:$DbPort/$SmokeDatabase"
    $environmentNames = @($SafetyEnvironment.Keys) + @("DATABASE_URL")
    foreach ($name in $environmentNames) {
        $SavedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }
    Set-SmokeEnvironment
    # Alembic needs the isolated host DATABASE_URL but must never enable any
    # execution mode. AUTO_PAPER_EXECUTION=true is reserved for uvicorn only.
    [Environment]::SetEnvironmentVariable("AUTO_PAPER_EXECUTION", "false", "Process")
    try {
        Invoke-NativeCommand -FilePath $Python `
            -Arguments @("-m", "alembic", "upgrade", "head") -Quiet -Sensitive `
            -RedactValues @($DbPassword, $EncodedPassword, $DatabaseUrl) | Out-Null
    }
    finally {
        Restore-Environment
    }

    $TestPort = Get-AvailablePort
    $BaseUrl = "http://127.0.0.1:$TestPort"
    $UvicornProcess = Start-LocalUvicorn
    $initialStatus = Wait-Api
    Assert-True ($initialStatus.mode -eq "PAPER") "BOT_MODE must be PAPER"
    Assert-True ($initialStatus.auto_paper_execution -eq $true) `
        "AUTO_PAPER_EXECUTION must be true"
    Assert-True ($initialStatus.order_placement_blocked -eq $true) `
        "exchange order placement must be blocked"
    Assert-True ($initialStatus.live_trading -eq $false) "live trading must be false"
    $initialEquity = Get-RequiredDecimalProperty `
        $initialStatus "paper_equity" "/status"
    Assert-DecimalNear -Actual $initialEquity -Expected ([decimal]10000) `
        -Tolerance ([decimal]0.000001) -Endpoint "/status" -Field "paper_equity"

    $unique = [guid]::NewGuid().ToString("N")
    $news = Invoke-Api POST "/news/test-item" @{
        title = "SEC approves spot Bitcoin ETF from BlackRock $unique"
        summary = "The approval removes a major barrier and supports institutional BTC adoption."
        source = "auto-paper-execution-smoke"
        url = "https://example.invalid/auto-paper/$unique"
        published_at = [DateTimeOffset]::UtcNow.ToString("o")
    }
    Assert-True ($news.accepted -eq $true) "test news was rejected"
    Assert-True ($news.classification.trade_eligible -eq $true) `
        "classification is not trade eligible"

    $signal = Invoke-Api POST "/signals/test-from-news" @{
        news_id = [string]$news.item.id
        reprocess = $false
    }
    Assert-True ($signal.results.Count -eq 1) "expected exactly one BTC candidate"
    $CandidateId = [string]$signal.results[0].candidate.id

    $confirmation = Invoke-Api POST "/signals/$CandidateId/test-market-snapshot" @{
        price = 60000.0
        bid = 59994.0
        ask = 60006.0
        price_change_1m_pct = 0.50
        trend_direction = "bullish"
        trend_score = 1.0
        volatility_pct = 0.10
        volume_24h = 1000000
        volume_change_pct = 25.0
        volume_spike = $true
        fresh = $true
    }
    $opened = Wait-CandidateState $CandidateId "PAPER_OPENED"
    $readyEvaluations = @($opened.candidate.evaluation_history | Where-Object {
        $_.state -eq "READY"
    })
    Assert-True ($readyEvaluations.Count -ge 1) `
        "candidate has no READY market-confirmation evaluation"
    Assert-True ($opened.candidate.final_action -eq "BUY") `
        "automatic execution lost BUY direction"
    Assert-True ($opened.risk_preview.preview_performed -eq $true) `
        "risk preview was not performed"
    Assert-True ($opened.risk_preview.approved -eq $true) `
        "risk preview was not approved"
    Write-Host "AUTO READY: PASS" -ForegroundColor Green

    Assert-True ($opened.execution_attempted -eq $true) `
        "automatic execution_attempted is not true"
    Assert-True ($opened.paper_position_opened -eq $true) `
        "automatic paper_position_opened is not true"
    Assert-True ($null -ne $opened.risk_preview.risk_decision_id) `
        "risk_decision_id is missing"
    $positions = Invoke-Api GET "/paper/positions"
    Assert-True ($positions.positions.Count -eq 1) `
        "expected exactly one open paper position"
    $position = $positions.positions[0]
    $PositionId = [string]$position.id
    Assert-True ([string]$position.candidate_id -eq $CandidateId) `
        "position is not linked to the candidate"
    Assert-True ($null -ne $position.risk_decision_id) `
        "position risk_decision_id is missing"
    Assert-True ((Get-PersistedCount `
        "SELECT count(*) FROM paper_executions WHERE candidate_id='$CandidateId'") -eq 1) `
        "expected exactly one paper execution record"
    Assert-True ((Get-PersistedCount `
        "SELECT count(*) FROM paper_positions WHERE id='$PositionId' AND status='OPEN'") -eq 1) `
        "expected exactly one persisted open paper position"
    Write-Host "AUTO PAPER_OPENED: PASS" -ForegroundColor Green

    1..3 | ForEach-Object {
        Invoke-Api POST "/signals/$CandidateId/test-market-snapshot" @{
            price = 60000.0
            bid = 59994.0
            ask = 60006.0
            price_change_1m_pct = 0.50
            trend_direction = "bullish"
            trend_score = 1.0
            volatility_pct = 0.10
            volume_24h = 1000000
            volume_change_pct = 25.0
            volume_spike = $true
            fresh = $true
        } | Out-Null
        $candidateCheck = Invoke-Api GET "/signals/$CandidateId"
        Assert-True ($candidateCheck.candidate.state -eq "PAPER_OPENED") `
            "candidate left PAPER_OPENED during duplicate check"
        Start-Sleep -Milliseconds 250
    }
    Assert-True ((Get-PersistedCount `
        "SELECT count(*) FROM paper_executions WHERE candidate_id='$CandidateId'") -eq 1) `
        "duplicate paper execution record was created"
    $positionsAfterRecheck = Invoke-Api GET "/paper/positions"
    Assert-True ($positionsAfterRecheck.positions.Count -eq 1) `
        "duplicate paper position was created"
    Assert-True ([string]$positionsAfterRecheck.positions[0].id -eq $PositionId) `
        "paper position ID changed during duplicate checks"
    Write-Host "AUTO DUPLICATE PROTECTION: PASS" -ForegroundColor Green

    $takeProfit = [decimal]$position.take_profit
    $closedResponse = Invoke-Api POST "/paper/test/market-snapshot" @{
        symbol = "BTCUSDT"
        price = $takeProfit + [decimal]0.01
        bid = $takeProfit
        ask = $takeProfit + [decimal]0.02
        timestamp = [DateTimeOffset]::UtcNow.ToString("o")
    }
    $closed = Wait-CandidateState $CandidateId "PAPER_CLOSED"
    Assert-True ($null -ne $closedResponse.position) "take profit did not return a position"
    Assert-True ($closedResponse.position.status -eq "CLOSED") `
        "take profit did not close the position"
    Assert-True ($closedResponse.position.close_reason.ToUpperInvariant() -eq "TAKE_PROFIT") `
        "close_reason is not TAKE_PROFIT"
    $openAfterClose = Invoke-Api GET "/paper/positions"
    $tradesAfterClose = Invoke-Api GET "/paper/trades"
    $pnlAfterClose = Invoke-Api GET "/paper/pnl"
    Assert-True ($openAfterClose.positions.Count -eq 0) `
        "an open position remains after take profit"
    Assert-True ($tradesAfterClose.trades.Count -eq 1) `
        "expected exactly one closed paper trade"
    $trade = $tradesAfterClose.trades[0]
    Assert-True ([string]$trade.id -eq $PositionId) "closed trade ID changed"
    $entryFee = Get-RequiredDecimalProperty `
        $trade "estimated_entry_fee" "/paper/trades"
    $exitFee = Get-RequiredDecimalProperty `
        $trade "estimated_exit_fee" "/paper/trades"
    $entrySlippage = Get-RequiredDecimalProperty `
        $trade "estimated_entry_slippage" "/paper/trades"
    $exitSlippage = Get-RequiredDecimalProperty `
        $trade "estimated_exit_slippage" "/paper/trades"
    $grossPnl = Get-RequiredDecimalProperty $trade "gross_pnl" "/paper/trades"
    $tradeFeesPaid = Get-RequiredDecimalProperty `
        $trade "fees_paid" "/paper/trades"
    $tradeSlippagePaid = Get-RequiredDecimalProperty `
        $trade "slippage_paid" "/paper/trades"
    $tradeRealizedPnl = Get-RequiredDecimalProperty `
        $trade "realized_pnl" "/paper/trades"
    $expectedFees = [decimal]$entryFee + [decimal]$exitFee
    $expectedSlippage = [decimal]$entrySlippage + [decimal]$exitSlippage
    $expectedNetPnl = [decimal]$grossPnl - $expectedFees - $expectedSlippage
    Assert-DecimalNear -Actual $tradeFeesPaid -Expected $expectedFees `
        -Tolerance ([decimal]0.000001) -Endpoint "/paper/trades" -Field "fees_paid"
    Assert-DecimalNear -Actual $tradeSlippagePaid -Expected $expectedSlippage `
        -Tolerance ([decimal]0.000001) -Endpoint "/paper/trades" -Field "slippage_paid"
    Assert-DecimalNear -Actual $tradeRealizedPnl -Expected $expectedNetPnl `
        -Tolerance ([decimal]0.000001) -Endpoint "/paper/trades" -Field "realized_pnl"
    $pnlRealized = Get-RequiredDecimalProperty `
        $pnlAfterClose "realized_pnl" "/paper/pnl"
    Assert-DecimalNear -Actual $pnlRealized -Expected $tradeRealizedPnl `
        -Tolerance ([decimal]0.000001) -Endpoint "/paper/pnl" -Field "realized_pnl"
    $canonicalStartingEquity = Get-RequiredDecimalProperty `
        $pnlAfterClose "starting_equity" "/paper/pnl"
    $canonicalEquity = Get-RequiredDecimalProperty `
        $pnlAfterClose "equity" "/paper/pnl"
    $expectedCanonicalEquity = $canonicalStartingEquity + $tradeRealizedPnl
    Assert-DecimalNear -Actual $canonicalEquity -Expected $expectedCanonicalEquity `
        -Tolerance ([decimal]0.000001) -Endpoint "/paper/pnl" -Field "equity"
    $closeResponseRealized = Get-RequiredDecimalProperty `
        $closedResponse.pnl "realized_pnl" "/paper/test/market-snapshot"
    Assert-DecimalNear -Actual $closeResponseRealized -Expected $tradeRealizedPnl `
        -Tolerance ([decimal]0.000001) `
        -Endpoint "/paper/test/market-snapshot" -Field "pnl.realized_pnl"
    Write-Host "AUTO TAKE_PROFIT: PASS" -ForegroundColor Green

    Stop-LocalUvicorn $UvicornProcess
    $UvicornProcess = $null
    $TestPort = Get-AvailablePort
    $BaseUrl = "http://127.0.0.1:$TestPort"
    $UvicornProcess = Start-LocalUvicorn
    $restartStatus = Wait-Api
    $restored = Wait-CandidateState $CandidateId "PAPER_CLOSED"
    Assert-True ($restored.candidate.state -eq "PAPER_CLOSED") `
        "closed candidate state was not restored"
    $restartPositions = Invoke-Api GET "/paper/positions"
    $restartTrades = Invoke-Api GET "/paper/trades"
    $restartPnl = Invoke-Api GET "/paper/pnl"
    Assert-True ($restartPositions.positions.Count -eq 0) `
        "closed candidate reopened after restart"
    Assert-True ($restartTrades.trades.Count -eq 1) `
        "closed trade was not restored exactly once"
    Assert-True ((Get-PersistedCount `
        "SELECT count(*) FROM paper_executions WHERE candidate_id='$CandidateId'") -eq 1) `
        "execution was duplicated after restart"
    Invoke-Api POST "/signals/$CandidateId/test-market-snapshot" @{
        price = 60000.0
        bid = 59994.0
        ask = 60006.0
        price_change_1m_pct = 0.50
        trend_direction = "bullish"
        trend_score = 1.0
        volatility_pct = 0.10
        volume_24h = 1000000
        volume_change_pct = 25.0
        volume_spike = $true
        fresh = $true
    } | Out-Null
    $restartPositionsAgain = Invoke-Api GET "/paper/positions"
    Assert-True ($restartPositionsAgain.positions.Count -eq 0) `
        "closed candidate reopened after a post-restart recheck"
    Assert-True ((Get-PersistedCount `
        "SELECT count(*) FROM paper_executions WHERE candidate_id='$CandidateId'") -eq 1) `
        "post-restart recheck duplicated the execution"
    $restartEquity = Get-RequiredDecimalProperty `
        $restartPnl "equity" "/paper/pnl (after restart)"
    Assert-DecimalNear -Actual $restartEquity -Expected $expectedCanonicalEquity `
        -Tolerance ([decimal]0.000001) `
        -Endpoint "/paper/pnl (after restart)" -Field "equity"
    $statusEquity = Get-RequiredDecimalProperty `
        $restartStatus "paper_equity" "/status (after restart)"
    Assert-DecimalNear -Actual $statusEquity -Expected $expectedCanonicalEquity `
        -Tolerance ([decimal]0.000001) `
        -Endpoint "/status (after restart)" -Field "paper_equity"
    Write-Host "RESTART RECOVERY: PASS" -ForegroundColor Green

    $finalStatus = Invoke-Api GET "/status"
    Assert-True ($finalStatus.order_placement_blocked -eq $true) `
        "exchange order placement is not blocked"
    Assert-True ($finalStatus.live_trading -eq $false) "live trading is not false"
    Assert-True ($closedResponse.exchange_order_placement -eq "blocked") `
        "paper monitoring response does not report exchange execution blocked"
    Write-Host "EXCHANGE EXECUTION BLOCKED: PASS" -ForegroundColor Green
}
catch {
    $Failure = $_.Exception.Message
    Write-FailureDiagnostics
}
finally {
    Stop-LocalUvicorn $UvicornProcess
    if ($DatabaseCreated -and $PostgresService -and $DbUser -and $SmokeDatabase) {
        try {
            Invoke-Docker @(
                "compose", "exec", "-T", $PostgresService,
                "dropdb", "-U", $DbUser, "--if-exists", $SmokeDatabase
            ) -Quiet -Sensitive | Out-Null
        } catch {
            Write-Warning "Could not remove the isolated smoke-test database."
        }
    }
    Restore-Environment
    if (Test-Path -LiteralPath $LogDirectory) {
        try {
            Remove-Item -LiteralPath $LogDirectory -Recurse -Force -ErrorAction Stop
        }
        catch {
            Write-Warning "Could not remove the temporary smoke-test log directory."
        }
    }
}

if ($Failure) {
    Write-Host "OVERALL: FAIL - $Failure" -ForegroundColor Red
    exit 1
}

Write-Host "OVERALL: PASS" -ForegroundColor Green
exit 0
