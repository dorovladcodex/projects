from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
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


def configure_v2_logging(path: str, *, level: str = "INFO") -> logging.Logger:
    target = Path(path); target.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("bybot.v2"); logger.setLevel(level.upper())
    if not logger.handlers:
        handler = RotatingFileHandler(target, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(JsonFormatter()); logger.addHandler(handler)
    return logger


def _sanitize_log_text(value: str) -> str:
    import re

    text = re.sub(r"https?://[^\s?]+\?\S+", "[REDACTED_URL]", value)
    return re.sub(
        r"(?i)(api[_-]?key|secret|signature|authorization)\s*[:=]\s*\S+",
        r"\1=[REDACTED]", text,
    )
