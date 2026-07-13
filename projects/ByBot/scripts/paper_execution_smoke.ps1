[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Failure = $null
$CandidateId = $null
$FirstExecutionResponse = $null

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "Assertion failed: $Message" }
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
        $parameters.Body = $Body | ConvertTo-Json -Depth 20 -Compress
    }
    Invoke-RestMethod @parameters
}

try {
    $status = Invoke-Api GET "/status"
    Assert-True ($status.mode -eq "PAPER") "BOT_MODE must be PAPER"
    Assert-True ($status.auto_paper_execution -eq $false) "AUTO_PAPER_EXECUTION must be false for this manual smoke test"
    Assert-True ($status.live_trading -eq $false) "live trading must remain disabled"
    Assert-True ($status.order_placement_blocked -eq $true) "exchange orders must be blocked"

    $unique = [guid]::NewGuid().ToString("N")
    $news = Invoke-Api POST "/news/test-item" @{
        title = "SEC closes Bitcoin investigation without enforcement $unique"
        summary = "Removal of a major regulatory barrier is materially bullish for BTC institutional demand."
        source = "paper-execution-smoke"
        url = "https://example.invalid/paper-execution/$unique"
        published_at = [DateTimeOffset]::UtcNow.ToString("o")
    }
    Assert-True ($news.accepted -eq $true) "test news was rejected"
    Assert-True ($news.classification.trade_eligible -eq $true) "classification is not trade eligible"

    $signal = Invoke-Api POST "/signals/test-from-news" @{
        news_id = [string]$news.item.id
        reprocess = $false
    }
    Assert-True ($signal.results.Count -eq 1) "expected one signal candidate"
    $CandidateId = [string]$signal.results[0].candidate.id

    $ready = Invoke-Api POST "/signals/$CandidateId/test-market-snapshot" @{
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
    Assert-True ($ready.candidate.state -eq "READY") "candidate did not become READY"
    Assert-True ($ready.risk_preview.approved -eq $true) "risk preview was not approved"
    Write-Host "READY: PASS" -ForegroundColor Green

    $FirstExecutionResponse = Invoke-Api POST "/paper/test/execute-candidate/$CandidateId"
    Assert-True ($FirstExecutionResponse.result.candidate.state -eq "PAPER_OPENED") "first execution did not open paper position"
    Assert-True ($FirstExecutionResponse.result.candidate.final_action -eq "BUY") "BUY direction was lost"
    Assert-True ($FirstExecutionResponse.execution_attempted -eq $true) "first execution was not attempted"
    Assert-True ($FirstExecutionResponse.paper_position_opened -eq $true) "first execution did not report opened position"
    Assert-True ($FirstExecutionResponse.exchange_order_placement -eq "blocked") "exchange execution was not blocked"
    $positionId = [string]$FirstExecutionResponse.position.id
    $takeProfit = [double]$FirstExecutionResponse.position.take_profit
    Write-Host "PAPER_OPENED: PASS" -ForegroundColor Green

    $second = Invoke-Api POST "/paper/test/execute-candidate/$CandidateId"
    Assert-True ($second.duplicate -eq $true) "second execution was not recognized as duplicate"
    Assert-True ($second.paper_position_opened -eq $false) "duplicate opened another position"
    Assert-True ($second.result.candidate.final_action -eq "BUY") "duplicate changed BUY to NO_TRADE"
    Assert-True ([string]$second.position.id -eq $positionId) "duplicate did not return existing position"
    Write-Host "IDEMPOTENT DUPLICATE: PASS" -ForegroundColor Green

    $closed = Invoke-Api POST "/paper/test/market-snapshot" @{
        symbol = "BTCUSDT"
        price = $takeProfit + 0.01
        bid = $takeProfit
        ask = $takeProfit + 0.02
        timestamp = [DateTimeOffset]::UtcNow.ToString("o")
    }
    Assert-True ($closed.position.status -eq "CLOSED") "take profit did not close position"
    Assert-True ($closed.position.close_reason -eq "take_profit") "close reason is not take_profit"
    Assert-True ($closed.exchange_order_placement -eq "blocked") "exchange execution was not blocked"
    $candidate = Invoke-Api GET "/signals/$CandidateId"
    Assert-True ($candidate.candidate.state -eq "PAPER_CLOSED") "candidate did not become PAPER_CLOSED"
    Write-Host "TAKE_PROFIT -> PAPER_CLOSED: PASS" -ForegroundColor Green
}
catch {
    $Failure = $_.Exception.Message
    Write-Host "--- FAILURE DIAGNOSTICS ---" -ForegroundColor Yellow
    Write-Host "candidate_id: $CandidateId"
    if ($null -ne $FirstExecutionResponse) {
        Write-Host "candidate_state: $($FirstExecutionResponse.result.candidate.state)"
        Write-Host "final_action: $($FirstExecutionResponse.result.candidate.final_action)"
        Write-Host "risk_decision_id: $($FirstExecutionResponse.risk_decision_id)"
        Write-Host "full_first_execution_response:"
        Write-Host ($FirstExecutionResponse | ConvertTo-Json -Depth 30)
        Write-Host "existing_execution_records:"
        Write-Host ($FirstExecutionResponse.execution_record | ConvertTo-Json -Depth 20)
        Write-Host "existing_paper_positions:"
        Write-Host ($FirstExecutionResponse.paper_positions | ConvertTo-Json -Depth 20)
    }
    elseif ($CandidateId) {
        try {
            $candidateDiagnostic = Invoke-Api GET "/signals/$CandidateId"
            $positionsDiagnostic = Invoke-Api GET "/paper/positions"
            Write-Host "candidate_state: $($candidateDiagnostic.candidate.state)"
            Write-Host "final_action: $($candidateDiagnostic.candidate.final_action)"
            Write-Host "risk_decision_id: $($candidateDiagnostic.risk_preview.risk_decision_id)"
            Write-Host "existing_execution_records: unavailable before first execution response"
            Write-Host "existing_paper_positions:"
            Write-Host ($positionsDiagnostic.positions | ConvertTo-Json -Depth 20)
        } catch {
            Write-Host "Additional diagnostics unavailable."
        }
    }
}

if ($Failure) {
    Write-Host "OVERALL: FAIL - $Failure" -ForegroundColor Red
    exit 1
}

Write-Host "OVERALL: PASS" -ForegroundColor Green
exit 0
