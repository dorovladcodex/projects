from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import subprocess

import app.main as main_module
import pytest
from app.config import Settings
from app.models import ClassificationStatus, ClassifierTestRequest, Sentiment
from app.news.classifier import (
    CodexCLINewsClassifier,
    CodexCLIProvider,
    parse_codex_cli_total_tokens,
)
from app.news.service import NewsService
from tests.test_llm_classifier import VALID_RESPONSE, news_item


NEUTRAL_RESPONSE = (
    VALID_RESPONSE.replace('"BULLISH"', '"NEUTRAL"')
    .replace("0.91", "0.5")
    .replace("Spot ETF approval supports institutional demand.", "Impact is ambiguous.")
)


class FakeRunner:
    def __init__(
        self,
        outputs: list[str | BaseException | tuple[int, str] | tuple[str, str, str]],
    ) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.schema_bytes: list[bytes] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), dict(kwargs)))
        schema_path = Path(command[command.index("--output-schema") + 1])
        result_path = Path(command[command.index("--output-last-message") + 1])
        self.schema_bytes.append(schema_path.read_bytes())
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        if isinstance(output, tuple):
            if len(output) == 3:
                content, stdout, stderr = output
                result_path.write_text(content, encoding="utf-8", newline="")
                return subprocess.CompletedProcess(command, 0, stdout, stderr)
            return subprocess.CompletedProcess(command, output[0], "", output[1])
        result_path.write_text(output, encoding="utf-8", newline="")
        return subprocess.CompletedProcess(command, 0, "", "")


def codex_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "news_classifier_mode": "codex_cli",
        "codex_cli_enabled": True,
        "codex_cli_path": "codex",
        "codex_cli_model": "gpt-5.4-mini",
        "codex_cli_fallback_model": "gpt-5.6-luna",
        "codex_cli_reasoning_effort": "low",
        "codex_cli_fallback_min_confidence": 0.75,
        "llm_max_retries": 0,
        "llm_backoff_base_seconds": 0,
        "llm_hourly_request_budget": 100,
        "llm_daily_request_budget": 100,
        "llm_daily_token_budget": 10000,
        "app_env": "local",
        "test_mode": True,
        "news_enable_rss": False,
        "bybit_api_key": None,
        "bybit_api_secret": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def codex_classifier(
    runner: FakeRunner,
    settings: Settings | None = None,
) -> CodexCLINewsClassifier:
    settings = settings or codex_settings()
    provider = CodexCLIProvider(
        executable=settings.codex_cli_path,
        model=settings.codex_cli_model,
        fallback_model=settings.codex_cli_fallback_model,
        reasoning_effort=settings.codex_cli_reasoning_effort,
        fallback_min_confidence=settings.codex_cli_fallback_min_confidence,
        runner=runner,
    )
    return CodexCLINewsClassifier(
        settings,
        provider,
        sleeper=lambda _seconds: None,
    )


def test_codex_cli_success_uses_safe_exact_process_controls() -> None:
    runner = FakeRunner([VALID_RESPONSE])
    result = codex_classifier(runner).classify(news_item())

    command, kwargs = runner.calls[0]
    assert result.classification_status == ClassificationStatus.SUCCESS
    assert result.trade_eligible is True
    assert result.model_name == "gpt-5.4-mini"
    assert command[:4] == ["codex", "exec", "-m", "gpt-5.4-mini"]
    assert "--skip-git-repo-check" in command
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--color") + 1] == "never"
    assert 'model_reasoning_effort="low"' in command
    assert command[-1] == "-"
    assert kwargs["shell"] is False
    assert kwargs["input"]
    assert kwargs["text"] is True
    assert runner.schema_bytes[0][:3] != b"\xef\xbb\xbf"


def test_codex_cli_cache_hit_avoids_subprocess() -> None:
    runner = FakeRunner([(VALID_RESPONSE, "tokens used\r\n9,108\r\n", "")])
    classifier = codex_classifier(runner)

    first = classifier.classify(news_item())
    second = classifier.classify(news_item())

    assert first.classification_status == ClassificationStatus.SUCCESS
    assert second.classification_status == ClassificationStatus.CACHE_HIT
    assert len(runner.calls) == 1
    metrics = classifier.metrics_payload()
    assert metrics["codex_cli_cache_hits"] == 1
    assert metrics["codex_cli_calls_count"] == 1
    assert metrics["codex_cli_total_tokens_today"] == 9108


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("tokens used\n9,108\n", 9108),
        ("tokens used\r\n9108\r\n", 9108),
        ("tokens used\n9 108\n", 9108),
    ],
)
def test_parse_codex_cli_total_tokens(output: str, expected: int) -> None:
    assert parse_codex_cli_total_tokens(output) == expected


def test_codex_cli_succeeds_when_token_summary_is_missing() -> None:
    classifier = codex_classifier(FakeRunner([VALID_RESPONSE]))

    result = classifier.classify(news_item())
    metrics = classifier.metrics_payload()

    assert result.classification_status == ClassificationStatus.SUCCESS
    assert result.codex_cli_total_tokens is None
    assert result.codex_cli_token_count_available is False
    assert metrics["codex_cli_token_count_available"] is False
    assert metrics["codex_cli_total_tokens_today"] == 0


def test_failed_codex_cli_process_does_not_invent_token_usage() -> None:
    classifier = codex_classifier(FakeRunner([(1, "tokens used\n9,108")]))

    result = classifier.classify(news_item())
    metrics = classifier.metrics_payload()

    assert result.classification_status == ClassificationStatus.FAILED
    assert metrics["failed_codex_cli_calls_count"] == 1
    assert metrics["codex_cli_total_tokens_last_call"] is None
    assert metrics["codex_cli_total_tokens_today"] == 0
    assert metrics["codex_cli_token_count_available"] is False


def test_codex_cli_uses_fallback_only_for_valid_ambiguous_result() -> None:
    runner = FakeRunner([NEUTRAL_RESPONSE, VALID_RESPONSE])
    classifier = codex_classifier(runner)
    result = classifier.classify(news_item())

    assert len(runner.calls) == 2
    assert runner.calls[0][0][3] == "gpt-5.4-mini"
    assert runner.calls[1][0][3] == "gpt-5.6-luna"
    assert runner.calls[0][1]["cwd"] != runner.calls[1][1]["cwd"]
    assert result.sentiment == Sentiment.BULLISH
    assert result.model_name == "gpt-5.6-luna"
    assert result.trade_eligible is True
    assert classifier.metrics_payload()["real_llm_calls_count"] == 2


def test_codex_cli_timeout_and_invalid_json_fail_without_fallback() -> None:
    timeout_runner = FakeRunner(
        [subprocess.TimeoutExpired(cmd="codex", timeout=1)]
    )
    timeout_result = codex_classifier(timeout_runner).classify(news_item())
    invalid_runner = FakeRunner(["not-json"])
    invalid_result = codex_classifier(invalid_runner).classify(news_item())

    assert timeout_result.classification_status == ClassificationStatus.FAILED
    assert timeout_result.error_code == "TIMEOUT"
    assert len(timeout_runner.calls) == 1
    assert invalid_result.classification_status == ClassificationStatus.FAILED
    assert invalid_result.error_code == "INVALID_JSON"
    assert len(invalid_runner.calls) == 1


def test_codex_cli_retry_after_timeout_never_escalates_to_fallback_model() -> None:
    runner = FakeRunner(
        [subprocess.TimeoutExpired(cmd="codex", timeout=1), NEUTRAL_RESPONSE]
    )
    classifier = codex_classifier(
        runner,
        codex_settings(llm_max_retries=1),
    )

    result = classifier.classify(news_item())

    assert len(runner.calls) == 2
    assert all(call[0][3] == "gpt-5.4-mini" for call in runner.calls)
    assert result.classification_status == ClassificationStatus.SUCCESS
    assert result.sentiment == Sentiment.NEUTRAL
    assert result.trade_eligible is False


def test_codex_cli_auth_or_invalid_executable_does_not_use_fallback() -> None:
    auth_runner = FakeRunner([(1, "authentication failed")])
    auth_result = codex_classifier(auth_runner).classify(news_item())
    missing_runner = FakeRunner([FileNotFoundError("codex not found")])
    missing_result = codex_classifier(missing_runner).classify(news_item())

    assert auth_result.classification_status == ClassificationStatus.FAILED
    assert auth_result.error_code == "PROVIDER_UNAVAILABLE"
    assert len(auth_runner.calls) == 1
    assert missing_result.classification_status == ClassificationStatus.FAILED
    assert missing_result.error_code == "PROVIDER_UNAVAILABLE"
    assert len(missing_runner.calls) == 1


def test_codex_cli_budget_exceeded_does_not_start_subprocess() -> None:
    runner = FakeRunner([VALID_RESPONSE])
    classifier = codex_classifier(
        runner,
        codex_settings(llm_daily_token_budget=100),
    )

    result = classifier.classify(news_item())

    assert result.classification_status == ClassificationStatus.FAILED
    assert result.error_code == "DAILY_TOKEN_BUDGET"
    assert runner.calls == []


def test_codex_cli_does_not_use_fallback_when_second_request_budget_is_exhausted() -> None:
    runner = FakeRunner([NEUTRAL_RESPONSE])
    classifier = codex_classifier(
        runner,
        codex_settings(llm_hourly_request_budget=1),
    )

    result = classifier.classify(news_item())

    assert len(runner.calls) == 1
    assert result.classification_status == ClassificationStatus.SUCCESS
    assert result.sentiment == Sentiment.NEUTRAL
    assert result.trade_eligible is False


def test_codex_cli_classifier_test_endpoint_has_no_execution_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = codex_settings()
    classifier = codex_classifier(FakeRunner([VALID_RESPONSE]), settings)
    news = NewsService(
        [],
        classifier,
        max_item_age=timedelta(minutes=60),
    )
    candidates_before = len(main_module.signal_candidate_service.candidates)
    paper_before = main_module.paper_trading_service.open_position
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "news_service", news)

    response = main_module.test_news_classifier(
        ClassifierTestRequest(
            title="SEC approves Bitcoin ETF",
            summary="BTC ETF approval.",
        )
    )

    assert response["classification"]["classification_status"] == "SUCCESS"
    assert response["news_stored"] is False
    assert response["signal_created"] is False
    assert response["execution_attempted"] is False
    assert response["paper_position_opened"] is False
    assert response["exchange_order_placement"] == "blocked"
    assert news.items == []
    assert len(main_module.signal_candidate_service.candidates) == candidates_before
    assert main_module.paper_trading_service.open_position == paper_before
