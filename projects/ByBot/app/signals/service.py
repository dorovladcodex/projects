from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.bybit.market_data import MarketDataService
from app.bybit.private import BybitAccountService
from app.config import Settings
from app.models import (
    Asset,
    MarketConfirmation,
    MarketSnapshot,
    NewsClassification,
    NewsItem,
    NewsSignalAction,
    NewsSignalCandidate,
    RiskContext,
    RiskDecision,
    Sentiment,
    Side,
    SimpleTrend,
    SignalAction,
    SignalDryRunResult,
    Symbol,
    TradeSignal,
)
from app.news.service import NewsService
from app.portfolio.paper_trading import PaperTradingService
from app.risk import RiskManager, RiskRules


class SignalCandidateService:
    """Build explainable signal and risk previews without executing anything."""

    def __init__(
        self,
        settings: Settings,
        news_service: NewsService,
        market_data: MarketDataService,
        account_service: BybitAccountService,
        paper_trading: PaperTradingService,
    ) -> None:
        self.settings = settings
        self.news_service = news_service
        self.market_data = market_data
        self.account_service = account_service
        self.paper_trading = paper_trading
        self.processed_news_ids: set[UUID] = set()
        self.results: list[SignalDryRunResult] = []
        self.risk_preview_approved_count = 0
        self.risk_preview_blocked_count = 0

    @property
    def candidates(self) -> list[NewsSignalCandidate]:
        self._expire_candidates()
        return [result.candidate for result in self.results]

    @property
    def last_result(self) -> SignalDryRunResult | None:
        self._expire_candidates()
        return self.results[-1] if self.results else None

    @property
    def no_trade_candidates_count(self) -> int:
        return sum(candidate.action == NewsSignalAction.NO_TRADE for candidate in self.candidates)

    def process_pending(self) -> list[SignalDryRunResult]:
        created: list[SignalDryRunResult] = []
        for classification in self.news_service.classifications:
            if classification.news_id not in self.processed_news_ids:
                created.extend(self.process_news_id(classification.news_id))
        return created

    def process_news_id(
        self,
        news_id: UUID,
        *,
        allow_reprocess: bool = False,
        now: datetime | None = None,
    ) -> list[SignalDryRunResult]:
        if news_id in self.processed_news_ids:
            if not allow_reprocess:
                return [result for result in self.results if result.candidate.news_id == news_id]
            if not self.settings.test_mode:
                raise PermissionError("explicit signal reprocessing requires TEST_MODE=true")

        news = next((item for item in self.news_service.items if item.id == news_id), None)
        classification = next(
            (item for item in self.news_service.classifications if item.news_id == news_id), None
        )
        if news is None or classification is None:
            raise ValueError("news item or classification not found")

        symbols = _symbols_for_asset(classification.asset)
        if not symbols:
            result = self._build_result(news, classification, None, now=now)
            new_results = [result]
        else:
            new_results = [
                self._build_result(news, classification, symbol, now=now) for symbol in symbols
            ]

        self.results.extend(new_results)
        self.processed_news_ids.add(news_id)
        for result in new_results:
            if result.risk_preview and result.risk_preview.approved:
                self.risk_preview_approved_count += 1
            else:
                self.risk_preview_blocked_count += 1
        return new_results

    def as_candidates_payload(self) -> list[dict[str, object]]:
        return [candidate.model_dump(mode="json") for candidate in self.candidates]

    def as_dry_run_payload(self) -> list[dict[str, object]]:
        self._expire_candidates()
        return [result.model_dump(mode="json") for result in self.results]

    def _build_result(
        self,
        news: NewsItem,
        classification: NewsClassification,
        symbol: Symbol | None,
        *,
        now: datetime | None,
    ) -> SignalDryRunResult:
        now = now or datetime.now(timezone.utc)
        snapshot = self.market_data.latest_snapshot(symbol) if symbol is not None else None
        confirmation = self._confirm_market(classification.sentiment, symbol, snapshot, now)
        reasons = list(confirmation.reasons)

        if classification.sentiment == Sentiment.NEUTRAL:
            reasons.append("neutral classification")
        if classification.confidence < self.settings.signal_min_classification_confidence:
            reasons.append("classification confidence below signal threshold")
        if news.importance < self.settings.signal_min_news_importance:
            reasons.append("news importance below signal threshold")
        if symbol is None:
            reasons.append("news asset cannot be mapped to a supported symbol")
        if self.paper_trading.open_position is not None:
            reasons.append("an open paper position already exists")

        expected_edge_bps = self._expected_edge_bps(news, classification, confirmation)
        if expected_edge_bps < self.settings.signal_min_expected_edge_bps:
            reasons.append("expected edge after costs is insufficient")

        directional_action = (
            NewsSignalAction.BUY
            if classification.sentiment == Sentiment.BULLISH
            else NewsSignalAction.SELL
            if classification.sentiment == Sentiment.BEARISH
            else NewsSignalAction.NO_TRADE
        )
        action = directional_action if not reasons else NewsSignalAction.NO_TRADE
        candidate = NewsSignalCandidate(
            news_id=news.id,
            symbol=symbol,
            action=action,
            sentiment=classification.sentiment,
            classification_confidence=classification.confidence,
            news_importance=news.importance,
            category=classification.category,
            urgency=classification.urgency,
            market_confirmation=confirmation,
            expected_edge_bps=expected_edge_bps,
            proposed_stop_loss_pct=self.settings.signal_default_stop_loss_pct,
            proposed_take_profit_pct=self.settings.signal_default_take_profit_pct,
            ttl_seconds=self.settings.signal_ttl_seconds,
            reasons=reasons or ["news direction confirmed by deterministic market checks"],
            created_at=now,
            expires_at=now + timedelta(seconds=self.settings.signal_ttl_seconds),
        )
        risk_preview = self._preview_risk(candidate, snapshot)
        return SignalDryRunResult(candidate=candidate, risk_preview=risk_preview)

    def _confirm_market(
        self,
        sentiment: Sentiment,
        symbol: Symbol | None,
        snapshot: MarketSnapshot | None,
        now: datetime,
    ) -> MarketConfirmation:
        if symbol is None or snapshot is None or self.market_data.status != "OK":
            return MarketConfirmation(reasons=["market data is unavailable"])

        age = now - snapshot.timestamp
        fresh = age <= timedelta(seconds=self.settings.signal_confirmation_window_seconds)
        reasons: list[str] = []
        if not fresh:
            reasons.append("market data is stale")
        if snapshot.spread_bps > self.settings.max_spread_bps:
            reasons.append("spread is too wide")
        if not 0 <= snapshot.volatility_pct <= 8.0:
            reasons.append("volatility is outside allowed range")

        direction_confirmed = False
        if sentiment == Sentiment.BULLISH:
            direction_confirmed = (
                snapshot.price_change_1m_pct > 0
                and snapshot.trend_score > 0
                and snapshot.simple_trend == SimpleTrend.BULLISH
            )
        elif sentiment == Sentiment.BEARISH:
            direction_confirmed = (
                snapshot.price_change_1m_pct < 0
                and snapshot.trend_score < 0
                and snapshot.simple_trend == SimpleTrend.BEARISH
            )
        if sentiment != Sentiment.NEUTRAL and not direction_confirmed:
            reasons.append("market direction conflicts with news")

        volume_change_pct = self._volume_change_pct(symbol)
        volume_spike = volume_change_pct >= 20 if volume_change_pct is not None else None
        return MarketConfirmation(
            available=True,
            fresh=fresh,
            direction_confirmed=direction_confirmed,
            price_change_1m_pct=snapshot.price_change_1m_pct,
            trend_direction=snapshot.simple_trend.value,
            trend_score=snapshot.trend_score,
            spread_bps=snapshot.spread_bps,
            volatility_pct=snapshot.volatility_pct,
            volume_24h=snapshot.volume_24h,
            volume_change_pct=volume_change_pct,
            volume_spike=volume_spike,
            reasons=reasons,
        )

    def _volume_change_pct(self, symbol: Symbol) -> float | None:
        history = self.market_data.history.get(symbol, [])
        if len(history) < 2:
            return None
        previous = history[-2].volume_24h
        current = history[-1].volume_24h
        if previous is None or current is None or previous <= 0:
            return None
        return (current - previous) / previous * 100

    def _expected_edge_bps(
        self,
        news: NewsItem,
        classification: NewsClassification,
        confirmation: MarketConfirmation,
    ) -> float:
        if not confirmation.available:
            return 0.0
        price_component = abs(confirmation.price_change_1m_pct or 0) * 100
        trend_component = abs(confirmation.trend_score or 0) * 20
        volume_component = 5.0 if confirmation.volume_spike else 0.0
        gross_edge = (
            price_component * classification.confidence
            + trend_component
            + news.importance * 5
            + volume_component
        )
        round_trip_cost = (self.settings.default_paper_fees_bps + self.settings.default_slippage_bps) * 2
        max_target_edge = self.settings.signal_default_take_profit_pct * 100
        return round(max(0.0, min(gross_edge - round_trip_cost, max_target_edge)), 4)

    def _preview_risk(
        self, candidate: NewsSignalCandidate, snapshot: MarketSnapshot | None
    ) -> RiskDecision:
        if snapshot is None:
            return RiskDecision(approved=False, reasons=list(candidate.reasons))

        is_trade = candidate.action in {NewsSignalAction.BUY, NewsSignalAction.SELL}
        signal = TradeSignal(
            action=SignalAction.TRADE if is_trade else SignalAction.NO_TRADE,
            symbol=snapshot.symbol,
            side=(
                Side.BUY if candidate.action == NewsSignalAction.BUY
                else Side.SELL if candidate.action == NewsSignalAction.SELL
                else None
            ),
            confidence=candidate.classification_confidence,
            expected_edge_bps=candidate.expected_edge_bps,
            stop_loss_pct=candidate.proposed_stop_loss_pct if is_trade else None,
            take_profit_pct=candidate.proposed_take_profit_pct if is_trade else None,
            reasons=list(candidate.reasons),
        )
        account = self.account_service.status
        context = RiskContext(
            equity=account.equity or self.settings.paper_starting_equity,
            available_balance=account.available_balance,
            requested_risk_pct=self.settings.max_risk_per_trade_pct,
            leverage=self.settings.max_leverage,
            open_positions=1 if self.paper_trading.open_position else 0,
            daily_pnl_pct=self.settings.paper_daily_pnl_pct,
            weekly_pnl_pct=self.settings.paper_weekly_pnl_pct,
            consecutive_losses=self.settings.paper_consecutive_losses,
            api_stable=self.market_data.status == "OK",
        )
        decision = RiskManager(_risk_rules(self.settings)).assess(signal, snapshot, context)
        if not is_trade:
            for reason in candidate.reasons:
                if reason not in decision.reasons:
                    decision.reasons.append(reason)
        return decision

    def _expire_candidates(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        for result in self.results:
            candidate = result.candidate
            if candidate.expires_at <= now and candidate.action != NewsSignalAction.NO_TRADE:
                candidate.action = NewsSignalAction.NO_TRADE
                candidate.reasons.append("signal expired")
                if result.risk_preview:
                    result.risk_preview.approved = False
                    result.risk_preview.reasons.append("signal expired")


def _symbols_for_asset(asset: Asset) -> tuple[Symbol, ...]:
    if asset == Asset.BTC:
        return (Symbol.BTCUSDT,)
    if asset == Asset.ETH:
        return (Symbol.ETHUSDT,)
    if asset == Asset.MARKET:
        return (Symbol.BTCUSDT, Symbol.ETHUSDT)
    return ()


def _risk_rules(settings: Settings) -> RiskRules:
    return RiskRules(
        max_risk_per_trade_pct=settings.max_risk_per_trade_pct,
        max_daily_loss_pct=settings.max_daily_loss_pct,
        max_weekly_loss_pct=settings.max_weekly_loss_pct,
        max_leverage=settings.max_leverage,
        max_spread_bps=settings.max_spread_bps,
        min_confidence=settings.signal_min_classification_confidence,
        min_expected_edge_bps=settings.signal_min_expected_edge_bps,
        max_position_notional_usdt=settings.max_position_notional_usdt,
        max_position_notional_pct_of_equity=settings.max_position_notional_pct_of_equity,
        min_position_notional_usdt=settings.min_position_notional_usdt,
        default_paper_fees_bps=settings.default_paper_fees_bps,
        default_slippage_bps=settings.default_slippage_bps,
        min_net_edge_bps=settings.min_net_edge_bps,
    )
