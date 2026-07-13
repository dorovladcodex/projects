from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.bybit.market_data import MarketDataService
from app.bybit.private import build_account_service
from app.config import Settings
from app.db.persistence import PersistenceRepository
from app.models import CandidateLifecycleState, MarketSnapshot, NewsItem, Symbol
from app.news.classifier import CodexCLINewsClassifier, CodexCLIProvider
from app.news.service import NewsService
from app.portfolio.paper_trading import PaperTradingService
from app.signals.service import SignalCandidateService
from tests.test_codex_cli_classifier import FakeRunner, VALID_RESPONSE


class BullishMarket:
    def __init__(self) -> None:
        self.calls = 0

    def get_snapshot(self, symbol: Symbol) -> MarketSnapshot:
        self.calls += 1
        price = 100 + self.calls * 0.3
        return MarketSnapshot(
            symbol=symbol, timestamp=datetime.now(timezone.utc), last_price=price,
            bid_price=price - 0.01, ask_price=price + 0.01,
            trend_score=0, volatility_pct=0, liquidity_ok=True, volume_24h=10_000,
        )


def settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None, database_url=database_url, news_classifier_mode="codex_cli",
        codex_cli_enabled=True, codex_cli_path="codex", news_enable_rss=False,
        bybit_api_key=None, bybit_api_secret=None, llm_max_retries=0,
        signal_min_expected_edge_bps=5, default_paper_fees_bps=1,
        default_slippage_bps=1,
    )


def classifier(config: Settings, runner: FakeRunner) -> CodexCLINewsClassifier:
    return CodexCLINewsClassifier(
        config,
        CodexCLIProvider(
            executable="codex", model=config.codex_cli_model,
            fallback_model=config.codex_cli_fallback_model,
            reasoning_effort=config.codex_cli_reasoning_effort,
            fallback_min_confidence=config.codex_cli_fallback_min_confidence,
            runner=runner,
        ),
        sleeper=lambda _: None,
    )


def test_durable_codex_to_ready_risk_preview_pipeline(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'pipeline.db'}"
    config = settings(database_url)
    repository = PersistenceRepository(database_url)
    runner = FakeRunner([(VALID_RESPONSE, "tokens used\n9,108\n", "")])
    news = NewsService(
        [], classifier(config, runner), max_item_age=timedelta(minutes=60),
        codex_min_news_importance=0.7, classifier_version="news-v1",
        repository=repository,
    )
    item = NewsItem(
        title="SEC investigation examines Bitcoin market structure",
        summary="The investigation may materially affect BTC institutional flows.",
        source="rss-test", url="https://example.test/story",
        published_at=datetime.now(timezone.utc),
    )
    accepted, _, classification_result = news.ingest(item)
    assert accepted and classification_result and classification_result.trade_eligible
    assert len(runner.calls) == 1

    market = MarketDataService(BullishMarket(), [Symbol.BTCUSDT])
    market.refresh_all()
    market.refresh_all()
    paper = PaperTradingService(starting_equity=config.paper_starting_equity_usdt)
    signals = SignalCandidateService(
        config, news, market, build_account_service(config), paper, repository,
    )
    result = signals.process_news_id(classification_result.news_id)[0]
    assert result.candidate.state == CandidateLifecycleState.READY
    assert result.risk_preview.preview_performed is True
    assert result.risk_preview.approved is True
    assert paper.open_position is None
    assert result.execution_attempted is False

    recreated_repository = PersistenceRepository(database_url)
    recreated_news = NewsService(
        [], classifier(config, FakeRunner([])), max_item_age=timedelta(minutes=60),
        repository=recreated_repository,
    )
    recreated_news.restore()
    recreated_signals = SignalCandidateService(
        config, recreated_news, market, build_account_service(config),
        PaperTradingService(starting_equity=config.paper_starting_equity_usdt),
        recreated_repository,
    )
    recreated_signals.restore()
    assert len(recreated_news.items) == 1
    assert len(recreated_news.classifications) == 1
    assert recreated_signals.candidates[0].state == CandidateLifecycleState.READY
    assert recreated_signals.paper_trading.open_position is None


def test_duplicate_and_expired_restart_recovery(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'recovery.db'}"
    config = settings(database_url)
    repository = PersistenceRepository(database_url)
    news = NewsService([], classifier(config, FakeRunner([VALID_RESPONSE])), max_item_age=timedelta(hours=1), repository=repository)
    item = NewsItem(
        title="SEC investigation examines Bitcoin market structure",
        summary="Material BTC market investigation.", source="rss",
        url="https://example.test/duplicate", published_at=datetime.now(timezone.utc),
    )
    assert news.ingest(item)[0] is True
    duplicate = item.model_copy(update={"id": NewsItem(title="x", summary="x", source="x", published_at=datetime.now(timezone.utc)).id})
    assert news.ingest(duplicate)[1] == "duplicate"
    assert news.news_duplicates_skipped == 1


def test_persistent_classifier_cache_hit_avoids_codex_call(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'cache.db'}"
    config = settings(database_url)
    repository = PersistenceRepository(database_url)
    first_runner = FakeRunner([VALID_RESPONSE])
    first_news = NewsService(
        [], classifier(config, first_runner), max_item_age=timedelta(hours=1),
        repository=repository,
    )
    item = NewsItem(
        title="SEC investigation examines Bitcoin market structure",
        summary="Material BTC market investigation.", source="rss",
        url="https://example.test/cache", published_at=datetime.now(timezone.utc),
    )
    accepted, _, original = first_news.ingest(item)
    assert accepted and original is not None
    cached = repository.cached_classification(item, "news-v1", datetime.now(timezone.utc))
    assert cached is not None
    assert cached.classification_status.value == "CACHE_HIT"
    assert len(first_runner.calls) == 1


def test_pending_candidate_is_expired_during_restart_recovery(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'expired.db'}"
    config = settings(database_url)
    repository = PersistenceRepository(database_url)
    news = NewsService(
        [], classifier(config, FakeRunner([VALID_RESPONSE])),
        max_item_age=timedelta(hours=1), repository=repository,
    )
    accepted, _, classification_result = news.ingest(NewsItem(
        title="SEC investigation examines Bitcoin market structure",
        summary="Material BTC market investigation.", source="rss",
        url="https://example.test/expired", published_at=datetime.now(timezone.utc),
    ))
    assert accepted and classification_result
    market = MarketDataService(BullishMarket(), [Symbol.BTCUSDT])
    market.refresh_all()  # one sample remains sideways
    paper = PaperTradingService(starting_equity=config.paper_starting_equity_usdt)
    signals = SignalCandidateService(
        config, news, market, build_account_service(config), paper, repository,
    )
    result = signals.process_news_id(classification_result.news_id)[0]
    result.candidate.state = CandidateLifecycleState.PENDING_CONFIRMATION
    result.candidate.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    repository.save_signal_result(result)

    recovered = SignalCandidateService(
        config, news, market, build_account_service(config), paper, repository,
    )
    recovered.restore()
    assert recovered.candidates[0].state == CandidateLifecycleState.EXPIRED
    assert paper.open_position is None


def test_database_unavailable_fails_safe() -> None:
    repository = PersistenceRepository(
        "postgresql://bybot:bybot@127.0.0.1:1/bybot", create_schema=False
    )
    assert repository.available is False
    assert repository.last_error


def test_failed_codex_classification_creates_no_signal_or_execution(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'failed.db'}"
    config = settings(database_url)
    repository = PersistenceRepository(database_url)
    news = NewsService(
        [], classifier(config, FakeRunner(["not-json"])),
        max_item_age=timedelta(hours=1), repository=repository,
    )
    accepted, _, failed = news.ingest(NewsItem(
        title="SEC investigation examines Bitcoin market structure",
        summary="Material BTC market investigation.", source="rss",
        published_at=datetime.now(timezone.utc),
    ))
    assert accepted and failed is not None
    assert failed.trade_eligible is False
    market = MarketDataService(BullishMarket(), [Symbol.BTCUSDT])
    paper = PaperTradingService(starting_equity=config.paper_starting_equity_usdt)
    signals = SignalCandidateService(
        config, news, market, build_account_service(config), paper, repository,
    )
    assert signals.process_pending() == []
    assert paper.open_position is None


def test_high_confidence_deterministic_result_skips_codex(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'deterministic.db'}"
    config = settings(database_url)
    runner = FakeRunner([])
    news = NewsService(
        [], classifier(config, runner), max_item_age=timedelta(hours=1),
        repository=PersistenceRepository(database_url),
    )
    accepted, _, result = news.ingest(NewsItem(
        title="SEC approves spot Bitcoin ETF",
        summary="BlackRock receives ETF approval.", source="rss",
        published_at=datetime.now(timezone.utc),
    ))
    assert accepted and result is not None
    assert result.trade_eligible is True
    assert result.provider_name == "mock"
    assert runner.calls == []
    assert news.news_skipped_before_codex_count == 1
