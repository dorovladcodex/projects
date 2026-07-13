[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot "auto_paper_execution_smoke.ps1") -HelpersOnly

$Failure = $null
$UvicornProcess = $null
$CandidateId = $null
$PositionId = $null
$PostgresService = $null
$DbUser = $null
$SmokeDatabase = $null
$DatabaseCreated = $false
$SavedEnvironment = @{}
$LogDirectory = Join-Path ([System.IO.Path]::GetTempPath()) `
    ("bybot-paper-stabilization-" + [guid]::NewGuid().ToString("N"))
$StdoutLog = Join-Path $LogDirectory "uvicorn.stdout.log"
$StderrLog = Join-Path $LogDirectory "uvicorn.stderr.log"

$SafetyEnvironment.APP_ENV = "local"
$SafetyEnvironment.TEST_MODE = "true"
$SafetyEnvironment.BOT_MODE = "PAPER"
$SafetyEnvironment.AUTO_PAPER_EXECUTION = "true"
$SafetyEnvironment.BYBIT_ENABLE_TRADING = "false"
$SafetyEnvironment.NEWS_CLASSIFIER_MODE = "mock"
$SafetyEnvironment.NEWS_ENABLE_RSS = "false"
$SafetyEnvironment.NEWS_POLL_INTERVAL_SECONDS = "3600"
$SafetyEnvironment.SIGNAL_REEVALUATION_INTERVAL_SECONDS = "60"
$SafetyEnvironment.MARKET_DATA_PROVIDER = "MOCK"
$SafetyEnvironment.PAPER_STARTING_EQUITY_USDT = "10000"
$SafetyEnvironment.PAPER_MAX_TOTAL_OPEN_POSITIONS = "1"
$SafetyEnvironment.PAPER_SYMBOL_COOLDOWN_SECONDS = "300"
$SafetyEnvironment.PAPER_GLOBAL_ENTRY_COOLDOWN_SECONDS = "300"
$SafetyEnvironment.PAPER_MAX_DAILY_NET_LOSS_PCT = "0.001"
$SafetyEnvironment.PAPER_MAX_WEEKLY_NET_LOSS_PCT = "0.001"
$SafetyEnvironment.PAPER_MAX_ACCOUNT_DRAWDOWN_PCT = "0.012"

function New-SmokeSignal {
    param(
        [Parameter(Mandatory = $true)][string]$AssetName,
        [Parameter(Mandatory = $true)][string]$AssetSymbol
    )
    $unique = [guid]::NewGuid().ToString("N")
    $news = Invoke-Api POST "/news/test-item" @{
        title = "SEC approves spot $AssetName ETF from BlackRock $unique"
        summary = "The approval supports institutional $AssetSymbol adoption."
        source = "paper-stabilization-smoke"
        url = "https://example.invalid/paper-stabilization/$unique"
        published_at = [DateTimeOffset]::UtcNow.ToString("o")
    }
    Assert-True ($news.accepted -eq $true) "$AssetSymbol test news was rejected"
    Assert-True ($news.classification.trade_eligible -eq $true) `
        "$AssetSymbol classification is not trade eligible"
    $signal = Invoke-Api POST "/signals/test-from-news" @{
        news_id = [string]$news.item.id
        reprocess = $false
    }
    Assert-True ($signal.results.Count -eq 1) `
        "expected exactly one $AssetSymbol signal candidate"
    return $signal.results[0]
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
    $SmokeDatabase = "bybot_stabilization_" + [guid]::NewGuid().ToString("N")
    Invoke-Docker @(
        "compose", "exec", "-T", $PostgresService,
        "createdb", "-U", $DbUser, $SmokeDatabase
    ) -Quiet | Out-Null
    $DatabaseCreated = $true

    $EncodedUser = [uri]::EscapeDataString($DbUser)
    $EncodedPassword = [uri]::EscapeDataString($DbPassword)
    $DatabaseUrl = "postgresql+psycopg://$EncodedUser`:$EncodedPassword@127.0.0.1:$DbPort/$SmokeDatabase"
    foreach ($name in @($SafetyEnvironment.Keys) + @("DATABASE_URL")) {
        $SavedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }
    Set-SmokeEnvironment
    [Environment]::SetEnvironmentVariable("AUTO_PAPER_EXECUTION", "false", "Process")
    try {
        Invoke-NativeCommand -FilePath $Python `
            -Arguments @("-m", "alembic", "upgrade", "head") -Quiet -Sensitive `
            -RedactValues @($DbPassword, $EncodedPassword, $DatabaseUrl) | Out-Null
    }
    finally { Restore-Environment }

    $TestPort = Get-AvailablePort
    $BaseUrl = "http://127.0.0.1:$TestPort"
    $UvicornProcess = Start-LocalUvicorn
    $initial = Wait-Api
    Assert-True ($initial.auto_paper_enabled -eq $true) `
        "automatic paper execution is not enabled"
    Assert-True ($initial.order_placement_blocked -eq $true) `
        "exchange order placement is not blocked"
    Assert-True ($initial.maximum_positions -eq 1) `
        "paper total position limit is not one"

    $btc = New-SmokeSignal "Bitcoin" "BTC"
    $CandidateId = [string]$btc.candidate.id
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
    $opened = Wait-CandidateState $CandidateId "PAPER_OPENED"
    Assert-True ($opened.execution_attempted -eq $true) `
        "automatic execution was not attempted"
    Assert-True ($opened.paper_position_opened -eq $true) `
        "automatic paper position was not opened"
    $positions = Invoke-Api GET "/paper/positions"
    Assert-True ($positions.positions.Count -eq 1) `
        "automatic execution did not create exactly one position"
    $PositionId = [string]$positions.positions[0].id
    Write-Host "AUTO EXECUTION: PASS" -ForegroundColor Green

    1..2 | ForEach-Object {
        Invoke-Api POST "/signals/$CandidateId/test-market-snapshot" @{
            price = 60000.0; bid = 59994.0; ask = 60006.0
            price_change_1m_pct = 0.50; trend_direction = "bullish"
            trend_score = 1.0; volatility_pct = 0.10; volume_24h = 1000000
            volume_change_pct = 25.0; volume_spike = $true; fresh = $true
        } | Out-Null
    }
    $ethLimited = New-SmokeSignal "Ethereum" "ETH"
    Assert-True ($ethLimited.candidate.state -eq "BLOCKED") `
        "maximum total position limit did not block ETH candidate"
    Assert-True ((Get-PersistedCount `
        "SELECT count(*) FROM paper_executions") -eq 1) `
        "duplicate execution record was created"
    Assert-True ((Invoke-Api GET "/paper/positions").positions.Count -eq 1) `
        "duplicate or second position was created"
    Write-Host "POSITION LIMITS: PASS" -ForegroundColor Green

    $cooldown = Invoke-Api GET "/status"
    Assert-True ($cooldown.cooldown_state.global_remaining_seconds -gt 0) `
        "global entry cooldown is not active"
    Write-Host "COOLDOWN: PASS" -ForegroundColor Green

    Invoke-Api POST "/paper/test/market-snapshot" @{
        symbol = "BTCUSDT"
        price = 59900.0
        bid = 59899.0
        ask = 59901.0
        timestamp = [DateTimeOffset]::UtcNow.ToString("o")
    } | Out-Null
    $lossStatus = Invoke-Api GET "/status"
    Assert-True ($lossStatus.kill_switch_active -eq $true) `
        "drawdown kill switch was not activated"
    Assert-True (($lossStatus.kill_switch_reasons -join " ") -match "drawdown") `
        "drawdown kill-switch reason is missing"
    Assert-True ($lossStatus.entries_allowed -eq $false) `
        "entries remain allowed after kill switch"
    $blockedAfterLoss = New-SmokeSignal "Ethereum" "ETH"
    Assert-True ($blockedAfterLoss.candidate.state -eq "BLOCKED") `
        "new candidate was not blocked by kill switch"
    Assert-True (($blockedAfterLoss.candidate.reasons -join " ") -match "drawdown") `
        "blocked candidate does not expose kill-switch reason"
    Write-Host "LOSS KILL SWITCH: PASS" -ForegroundColor Green

    $openBeforeClose = (Invoke-Api GET "/paper/positions").positions[0]
    $stopLoss = [decimal]$openBeforeClose.stop_loss
    Invoke-Api POST "/paper/test/market-snapshot" @{
        symbol = "BTCUSDT"
        price = $stopLoss - [decimal]1.0
        bid = $stopLoss - [decimal]1.1
        ask = $stopLoss - [decimal]0.9
        timestamp = [DateTimeOffset]::UtcNow.ToString("o")
    } | Out-Null
    $closedStatus = Invoke-Api GET "/status"
    $closedTrades = Invoke-Api GET "/paper/trades"
    Assert-True ($closedStatus.kill_switch_active -eq $true) `
        "kill switch did not remain latched after close"
    Assert-True ([int]$closedStatus.open_positions -eq 0) `
        "position did not close while entries were blocked"
    Assert-True ($closedTrades.trades.Count -eq 1) `
        "closed trade count is not one"
    Assert-True ($closedTrades.trades[0].close_reason -eq "stop_loss") `
        "existing position did not close by stop loss"
    Assert-True ($null -ne $closedStatus.cooldown_state.symbols.BTCUSDT) `
        "BTC symbol cooldown was not recorded after close"
    Write-Host "CLOSE WHILE BLOCKED: PASS" -ForegroundColor Green

    Stop-LocalUvicorn $UvicornProcess
    $UvicornProcess = $null
    $TestPort = Get-AvailablePort
    $BaseUrl = "http://127.0.0.1:$TestPort"
    $UvicornProcess = Start-LocalUvicorn
    $restart = Wait-Api
    $restoredCandidate = Wait-CandidateState $CandidateId "PAPER_CLOSED"
    Assert-True ($restart.kill_switch_active -eq $true) `
        "kill switch was not restored"
    Assert-True ($restart.entries_allowed -eq $false) `
        "entries became allowed after restart"
    Assert-True ([int]$restart.open_positions -eq 0) `
        "closed position reopened after restart"
    Assert-True ((Invoke-Api GET "/paper/trades").trades.Count -eq 1) `
        "closed trade was not restored exactly once"
    Assert-True ((Get-PersistedCount `
        "SELECT count(*) FROM paper_executions") -eq 1) `
        "execution was duplicated after restart"
    Assert-True ($restoredCandidate.candidate.state -eq "PAPER_CLOSED") `
        "candidate closed state was not restored"
    Write-Host "RESTART RECOVERY: PASS" -ForegroundColor Green

    Assert-True ($restart.order_placement_blocked -eq $true) `
        "exchange execution is not blocked"
    Assert-True ($restart.live_trading -eq $false) "live trading is not false"
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
        }
        catch { Write-Warning "Could not remove the isolated smoke-test database." }
    }
    Restore-Environment
    if (Test-Path -LiteralPath $LogDirectory) {
        try { Remove-Item $LogDirectory -Recurse -Force -ErrorAction Stop }
        catch { Write-Warning "Could not remove the temporary smoke-test logs." }
    }
}

if ($Failure) {
    Write-Host "OVERALL: FAIL - $Failure" -ForegroundColor Red
    exit 1
}

Write-Host "OVERALL: PASS" -ForegroundColor Green
exit 0
