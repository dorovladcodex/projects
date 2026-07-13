[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$BaseUrl = "http://127.0.0.1:8765"
$UvicornProcess = $null
$Failure = $null
$StartedAt = Get-Date
$LogDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("bybot-smoke-" + [guid]::NewGuid().ToString("N"))
$StdoutLog = Join-Path $LogDirectory "uvicorn.stdout.log"
$StderrLog = Join-Path $LogDirectory "uvicorn.stderr.log"

$SafetyEnvironment = @{
    APP_ENV = "local"
    TEST_MODE = "true"
    BOT_MODE = "PAPER"
    NEWS_CLASSIFIER_MODE = "codex_cli"
    CODEX_CLI_ENABLED = "true"
    NEWS_ENABLE_RSS = "false"
    MARKET_DATA_PROVIDER = "MOCK"
    PAPER_STARTING_EQUITY_USDT = "10000"
    MAX_POSITION_NOTIONAL_PCT_OF_EQUITY = "5"
    MAX_POSITION_NOTIONAL_USDT = "5000"
    AUTO_PAPER_EXECUTION = "false"
    BYBIT_ENABLE_TRADING = "false"
}
$SavedEnvironment = @{}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [switch]$Quiet,
        [switch]$Sensitive,
        [string[]]$RedactValues = @()
    )

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    $previousErrorActionPreference = $ErrorActionPreference
    $exitCode = $null
    try {
        # Windows PowerShell 5.1 wraps native stderr as ErrorRecord objects when
        # redirected into the success stream. Separate files avoid that behavior.
        $ErrorActionPreference = "Continue"
        & $FilePath @Arguments 1> $stdoutPath 2> $stderrPath
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    $stdout = if (Test-Path -LiteralPath $stdoutPath) {
        [System.IO.File]::ReadAllText($stdoutPath)
    } else { "" }
    $stderr = if (Test-Path -LiteralPath $stderrPath) {
        [System.IO.File]::ReadAllText($stderrPath)
    } else { "" }
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue

    $safeStdout = $stdout
    $safeStderr = $stderr
    foreach ($value in $RedactValues) {
        if ($value) {
            $safeStdout = $safeStdout.Replace($value, "***")
            $safeStderr = $safeStderr.Replace($value, "***")
        }
    }

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

function Test-NativeCommandHandling {
    $currentPowerShell = (Get-Process -Id $PID).Path
    $success = Invoke-NativeCommand -FilePath $currentPowerShell -Arguments @(
        "-NoProfile", "-Command",
        "[Console]::Error.WriteLine('validation stderr success'); exit 0"
    ) -Quiet
    Assert-True ($success.ExitCode -eq 0) "native stderr with exit code 0 was treated as failure"
    Assert-True ($success.StdErr -match "validation stderr success") "native stderr was not captured"

    $failedAsExpected = $false
    try {
        Invoke-NativeCommand -FilePath $currentPowerShell -Arguments @(
            "-NoProfile", "-Command",
            "[Console]::Error.WriteLine('validation stderr failure'); exit 1"
        ) -Quiet | Out-Null
    }
    catch {
        $failedAsExpected = $_.Exception.Message -match "exit code 1"
    }
    Assert-True $failedAsExpected "native exit code 1 did not produce a failure"
}

function Invoke-Docker {
    param(
        [string[]]$Arguments,
        [switch]$Quiet,
        [switch]$Sensitive
    )
    return Invoke-NativeCommand -FilePath "docker" -Arguments $Arguments `
        -Quiet:$Quiet -Sensitive:$Sensitive
}

function Invoke-Api {
    param(
        [ValidateSet("GET", "POST")][string]$Method,
        [string]$Path,
        [object]$Body = $null
    )
    $parameters = @{
        Method = $Method
        Uri = "$BaseUrl$Path"
        TimeoutSec = 180
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json"
        $parameters.Body = ($Body | ConvertTo-Json -Depth 20 -Compress)
    }
    return Invoke-RestMethod @parameters
}

function Wait-Api {
    param([int]$TimeoutSeconds = 180)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $status = Invoke-RestMethod -Uri "$BaseUrl/status" -TimeoutSec 5
            if ($null -ne $status) { return $status }
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    throw "FastAPI did not become ready within $TimeoutSeconds seconds."
}

function Start-LocalUvicorn {
    Assert-True (Test-Path -LiteralPath $Python) "virtual environment Python is missing"
    $arguments = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8765")
    return Start-Process -FilePath $Python -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog
}

function Stop-LocalUvicorn {
    param($Process)
    if ($null -ne $Process -and -not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $Process.Id -Timeout 15 -ErrorAction SilentlyContinue
    }
}

function Get-PostgresService {
    $result = Invoke-Docker @("compose", "config", "--format", "json") -Quiet
    $json = $result.StdOut
    $config = $json | ConvertFrom-Json
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
            $health = (Invoke-Docker @("inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", $containerId) -Quiet).StdOut.Trim()
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
    $value = (Invoke-Docker @("compose", "exec", "-T", $Service, "printenv", $Name) -Quiet -Sensitive).StdOut.Trim()
    Assert-True ([bool]$value) "PostgreSQL environment variable $Name is unavailable"
    return $value
}

function Get-PersistedCount {
    param([string]$Service, [string]$User, [string]$Database, [string]$Sql)
    $raw = (Invoke-Docker @("compose", "exec", "-T", $Service, "psql", "-U", $User, "-d", $Database, "-tAc", $Sql) -Quiet).StdOut.Trim()
    $count = 0
    Assert-True ([int]::TryParse($raw, [ref]$count)) "PostgreSQL count query returned a non-numeric result"
    return $count
}

try {
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
    Test-NativeCommandHandling
    Assert-True ($null -ne (Get-Command docker -ErrorAction SilentlyContinue)) "Docker CLI is unavailable"
    Invoke-Docker @("--version") | Out-Null
    Write-Host "Docker CLI: PASS" -ForegroundColor Green
    Invoke-Docker @("compose", "version") | Out-Null
    Assert-True (Test-Path -LiteralPath $Python) "run local setup before the smoke test"
    Invoke-NativeCommand -FilePath $Python -Arguments @("-m", "alembic", "--help") -Quiet | Out-Null
    Assert-True ($null -ne (Get-Command codex -ErrorAction SilentlyContinue)) "authenticated Codex CLI is unavailable"
    Invoke-NativeCommand -FilePath "codex" -Arguments @("--version") | Out-Null

    $PostgresService = Get-PostgresService
    Invoke-Docker @("compose", "up", "-d", "--no-deps", $PostgresService) | Out-Null
    Write-Host "PostgreSQL container: PASS" -ForegroundColor Green
    Wait-PostgresHealthy -Service $PostgresService
    Write-Host "PostgreSQL health: PASS" -ForegroundColor Green

    $DbUser = Get-ContainerEnvironmentValue -Service $PostgresService -Name "POSTGRES_USER"
    $DbPassword = Get-ContainerEnvironmentValue -Service $PostgresService -Name "POSTGRES_PASSWORD"
    $DbName = Get-ContainerEnvironmentValue -Service $PostgresService -Name "POSTGRES_DB"
    $PortLine = (Invoke-Docker @("compose", "port", $PostgresService, "5432") -Quiet).StdOut.Trim()
    Assert-True ($PortLine -match ":(?<port>\d+)$") "could not detect the published PostgreSQL port"
    $DbPort = $Matches.port
    $EncodedUser = [uri]::EscapeDataString($DbUser)
    $EncodedPassword = [uri]::EscapeDataString($DbPassword)
    $DatabaseUrl = "postgresql+psycopg://$EncodedUser`:$EncodedPassword@127.0.0.1:$DbPort/$DbName"

    foreach ($name in $SafetyEnvironment.Keys + @("DATABASE_URL")) {
        $SavedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }
    foreach ($entry in $SafetyEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
    }
    [Environment]::SetEnvironmentVariable("DATABASE_URL", $DatabaseUrl, "Process")

    Invoke-NativeCommand -FilePath $Python `
        -Arguments @("-m", "alembic", "upgrade", "head") `
        -RedactValues @($DbPassword, $EncodedPassword, $DatabaseUrl) | Out-Null
    Write-Host "Alembic migration: PASS" -ForegroundColor Green

    $UvicornProcess = Start-LocalUvicorn
    $InitialStatus = Wait-Api
    Assert-True ($InitialStatus.persistence_status -eq "OK") "application persistence is unavailable"
    Assert-True ($InitialStatus.paper_starting_equity_usdt -eq 10000) "paper starting equity is not 10000 USDT"
    Assert-True ($InitialStatus.paper_account_equity -eq 10000) "paper equity did not start at 10000 USDT"
    $PaperRowsBefore = Get-PersistedCount $PostgresService $DbUser $DbName "SELECT count(*) FROM paper_positions"

    $UniqueId = [guid]::NewGuid().ToString("N")
    $PublishedAt = [DateTimeOffset]::UtcNow.ToString("o")
    $NewsResponse = Invoke-Api -Method POST -Path "/news/test-item" -Body @{
        title = "SEC closes Bitcoin investigation without enforcement $UniqueId"
        summary = "The removal of a major regulatory barrier is materially bullish for BTC institutional demand."
        source = "postgres-e2e-smoke"
        url = "https://example.invalid/bybot-smoke/$UniqueId"
        published_at = $PublishedAt
    }
    Assert-True ($NewsResponse.accepted -eq $true) "test news was rejected"
    Assert-True ($null -ne $NewsResponse.classification) "Codex classification was not created"
    Assert-True ($NewsResponse.classification.trade_eligible -eq $true) "Codex classification is not trade eligible"
    Assert-True ($NewsResponse.classification.provider_name -eq "codex-cli") "normal pipeline did not use Codex CLI"
    $NewsId = [string]$NewsResponse.item.id

    $SignalResponse = Invoke-Api -Method POST -Path "/signals/test-from-news" -Body @{
        news_id = $NewsId
        reprocess = $false
    }
    Assert-True ($SignalResponse.results.Count -eq 1) "expected exactly one BTC signal candidate"
    $CandidateId = [string]$SignalResponse.results[0].candidate.id

    $ReadyResponse = Invoke-Api -Method POST -Path "/signals/$CandidateId/test-market-snapshot" -Body @{
        price = 100.0
        bid = 99.99
        ask = 100.01
        price_change_1m_pct = 0.50
        trend_direction = "bullish"
        trend_score = 1.0
        volatility_pct = 0.10
        volume_24h = 1000000
        volume_change_pct = 25.0
        volume_spike = $true
        fresh = $true
    }
    Assert-True ($ReadyResponse.candidate.state -eq "READY") "candidate did not become READY"
    Assert-True ($ReadyResponse.candidate.final_action -eq "BUY") "READY candidate is not BUY"
    Assert-True ($ReadyResponse.risk_preview.preview_performed -eq $true) "risk preview was not performed"
    Assert-True ($ReadyResponse.risk_preview.approved -eq $true) "risk preview was not approved"
    Assert-True ($ReadyResponse.risk_preview.max_allowed_notional -eq 500) "risk preview did not size from 10000 USDT paper equity"
    Assert-True ($ReadyResponse.execution_attempted -eq $false) "execution was attempted"
    Assert-True ($ReadyResponse.paper_position_opened -eq $false) "paper position was opened"
    Assert-True ($ReadyResponse.exchange_order_placement -eq "blocked") "exchange order placement was not blocked"

    $PaperBeforeRestart = Invoke-Api -Method GET -Path "/paper/positions"
    Assert-True ($PaperBeforeRestart.positions.Count -eq 0) "paper position exists before restart"
    $HistoryBeforeRestart = Invoke-Api -Method GET -Path "/signals/history"
    $OriginalHistory = @($HistoryBeforeRestart.history | Where-Object { $_.candidate_id -eq $CandidateId })
    Assert-True ($OriginalHistory.Count -eq 1) "candidate evaluation history was not created"
    Assert-True ($OriginalHistory[0].evaluations.Count -gt 0) "candidate evaluation history is empty"

    Stop-LocalUvicorn -Process $UvicornProcess
    $UvicornProcess = Start-LocalUvicorn
    $RestartStatus = Wait-Api
    Assert-True ($RestartStatus.paper_account_equity -eq 10000) "paper equity changed after restart"

    $NewsAfter = Invoke-Api -Method GET -Path "/news"
    Assert-True (@($NewsAfter.items | Where-Object { $_.id -eq $NewsId }).Count -eq 1) "news was not restored"
    $ClassificationsAfter = Invoke-Api -Method GET -Path "/news/classifications"
    Assert-True (@($ClassificationsAfter.classifications | Where-Object { $_.news_id -eq $NewsId }).Count -eq 1) "classification was not restored"
    $CandidatesAfter = Invoke-Api -Method GET -Path "/signals/candidates"
    $MatchingCandidates = @($CandidatesAfter.candidates | Where-Object { $_.news_id -eq $NewsId })
    Assert-True ($MatchingCandidates.Count -eq 1) "candidate is missing or duplicated after restart"
    Assert-True ($MatchingCandidates[0].id -eq $CandidateId) "candidate ID changed after restart"
    $HistoryAfter = Invoke-Api -Method GET -Path "/signals/history"
    $RestoredHistory = @($HistoryAfter.history | Where-Object { $_.candidate_id -eq $CandidateId })
    Assert-True ($RestoredHistory.Count -eq 1) "evaluation history was not restored"
    Assert-True ($RestoredHistory[0].evaluations.Count -gt 0) "restored evaluation history is empty"
    $PaperAfterRestart = Invoke-Api -Method GET -Path "/paper/positions"
    Assert-True ($PaperAfterRestart.positions.Count -eq 0) "paper position exists after restart"

    Assert-True ((Get-PersistedCount $PostgresService $DbUser $DbName "SELECT count(*) FROM news_items WHERE id='$NewsId'") -eq 1) "news row is missing in PostgreSQL"
    Assert-True ((Get-PersistedCount $PostgresService $DbUser $DbName "SELECT count(*) FROM news_classifications WHERE news_id='$NewsId'") -eq 1) "classification row is missing in PostgreSQL"
    Assert-True ((Get-PersistedCount $PostgresService $DbUser $DbName "SELECT count(*) FROM signal_candidates WHERE id='$CandidateId'") -eq 1) "candidate row is missing in PostgreSQL"
    Assert-True ((Get-PersistedCount $PostgresService $DbUser $DbName "SELECT count(*) FROM signal_evaluations WHERE candidate_id='$CandidateId'") -gt 0) "evaluation rows are missing in PostgreSQL"
    Assert-True ((Get-PersistedCount $PostgresService $DbUser $DbName "SELECT count(*) FROM risk_decisions WHERE candidate_id='$CandidateId'") -gt 0) "risk decision row is missing in PostgreSQL"
    Assert-True ((Get-PersistedCount $PostgresService $DbUser $DbName "SELECT count(*) FROM paper_positions") -eq $PaperRowsBefore) "paper position rows changed during dry-run"
}
catch {
    $Failure = $_.Exception.Message
}
finally {
    Stop-LocalUvicorn -Process $UvicornProcess
    foreach ($name in $SavedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $SavedEnvironment[$name], "Process")
    }
    if (Test-Path -LiteralPath $LogDirectory) {
        Remove-Item -LiteralPath $LogDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$Duration = [math]::Round(((Get-Date) - $StartedAt).TotalSeconds, 1)
if ($Failure) {
    Write-Host "FAIL - PostgreSQL E2E smoke test ($Duration s): $Failure" -ForegroundColor Red
    exit 1
}

Write-Host "PASS - PostgreSQL persistence, Codex pipeline, READY risk preview, restart recovery, and execution safety verified ($Duration s)." -ForegroundColor Green
exit 0
