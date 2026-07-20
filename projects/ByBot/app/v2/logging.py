from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event_timestamp = getattr(record, "event_timestamp", None)
        payload: dict[str, Any] = {
            "timestamp": _json_timestamp(
                event_timestamp
                if event_timestamp is not None
                else datetime.fromtimestamp(record.created, tz=timezone.utc)
            ),
            "level": record.levelname, "logger": record.name,
            "message": record.getMessage(),
        }
        for name in (
            "run_id", "execution_id", "candidate_id", "strategy", "symbol",
            "event_type", "execution_environment", "error_category",
            "processing_stage", "source", "traceback_fingerprint", "cycle_id",
        ):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["traceback"] = _sanitize_log_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def _json_timestamp(value: datetime | str) -> str:
    stamp = value
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if not isinstance(stamp, datetime):
        return str(stamp)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        # Never mislabel a naive/local wall clock as UTC.
        return stamp.isoformat()
    return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def configure_v2_logging(path: str, *, level: str = "INFO") -> logging.Logger:
    target = Path(path).resolve(); target.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("bybot.v2"); logger.setLevel(level.upper())
    managed = [
        handler for handler in logger.handlers
        if getattr(handler, "_bybot_v2_managed", False)
    ]
    if not any(
        Path(getattr(handler, "baseFilename", "")).resolve() == target
        for handler in managed
    ):
        for handler in managed:
            logger.removeHandler(handler)
            handler.close()
        handler = RotatingFileHandler(target, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        handler._bybot_v2_managed = True  # type: ignore[attr-defined]
        handler.setFormatter(JsonFormatter()); logger.addHandler(handler)
    return logger


def _sanitize_log_text(value: str) -> str:
    import re

    text = re.sub(r"https?://[^\s?]+\?\S+", "[REDACTED_URL]", value)
    return re.sub(
        r"(?i)(api[_-]?key|secret|signature|authorization)\s*[:=]\s*\S+",
        r"\1=[REDACTED]", text,
    )
