from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from zoneinfo import ZoneInfo

from app.v2.logging import JsonFormatter, _json_timestamp


def _formatted_timestamp(event_timestamp: datetime) -> str:
    record = logging.LogRecord(
        "bybot.v2", logging.INFO, __file__, 1, "event", (), None
    )
    record.event_timestamp = event_timestamp
    return json.loads(JsonFormatter().format(record))["timestamp"]


def test_utc_and_berlin_timestamps_serialize_as_actual_utc() -> None:
    utc = datetime(2026, 7, 20, 9, 15, 25, 383788, tzinfo=timezone.utc)
    berlin = utc.astimezone(ZoneInfo("Europe/Berlin"))
    assert _json_timestamp(utc) == "2026-07-20T09:15:25.383788Z"
    assert _json_timestamp(berlin) == "2026-07-20T09:15:25.383788Z"
    assert _formatted_timestamp(berlin) == "2026-07-20T09:15:25.383788Z"


def test_naive_local_timestamp_is_never_marked_with_z() -> None:
    naive = datetime(2026, 7, 20, 11, 15, 25)
    assert _json_timestamp(naive) == "2026-07-20T11:15:25"
    assert not _formatted_timestamp(naive).endswith("Z")


def test_shared_event_timestamp_is_identical_across_artifact_shapes() -> None:
    occurred_at = datetime(
        2026, 7, 20, 9, 15, 25, 383788, tzinfo=timezone.utc
    )
    event_jsonl_timestamp = _formatted_timestamp(occurred_at)
    incident_timestamp = _json_timestamp(occurred_at)
    summary_timestamp = _json_timestamp(occurred_at)
    assert event_jsonl_timestamp == incident_timestamp == summary_timestamp
