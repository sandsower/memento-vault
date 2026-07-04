import json
from datetime import datetime, timezone

import pytest

from memento import telemetry


def test_parse_timestamp_utc_normalizes_naive_z_and_offsets():
    naive = telemetry.parse_timestamp_utc("2026-06-30T12:00:00")
    zulu = telemetry.parse_timestamp_utc("2026-06-30T12:00:00Z")
    offset = telemetry.parse_timestamp_utc("2026-06-30T08:00:00-04:00")

    assert naive == datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    assert zulu == datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    assert offset == datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def test_iter_recent_jsonl_handles_mixed_timestamp_awareness(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-06-30T11:59:00", "action": "naive"}),
                json.dumps({"ts": "2026-06-30T12:00:00Z", "action": "zulu"}),
                json.dumps({"ts": "2026-06-30T08:01:00-04:00", "action": "offset"}),
                json.dumps({"ts": "2026-06-30T11:00:00+00:00", "action": "old"}),
                "not json",
                json.dumps(["not", "object"]),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cutoff = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    recent = list(telemetry.iter_recent_jsonl(path, cutoff))

    assert [entry["action"] for entry in recent] == ["zulu", "offset"]


def test_iter_recent_jsonl_accepts_naive_cutoff_as_utc(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({"ts": "2026-06-30T12:00:00Z", "action": "match"}) + "\n", encoding="utf-8")

    cutoff = datetime(2026, 6, 30, 11, 59, 59)

    assert [entry["action"] for entry in telemetry.iter_recent_jsonl(path, cutoff)] == ["match"]


def test_iter_jsonl_raises_on_missing_input(tmp_path):
    missing = tmp_path / "missing.jsonl"

    with pytest.raises(FileNotFoundError):
        list(telemetry.iter_jsonl(missing))


def test_failure_rate_warning_threshold_is_shared():
    assert telemetry.HEALTH_MIN_EVENTS_FOR_RATE == 3
    assert telemetry.HEALTH_WARN_FAILURE_RATIO == 0.5
    assert telemetry.failure_rate(2, 3) == 0.6667
    assert telemetry.failure_rate_warns(2, 3) is True
    assert telemetry.failure_rate_warns(1, 2) is False


def test_format_timestamp_utc_handles_naive_and_aware_values():
    assert telemetry.format_timestamp_utc("2026-06-30T08:00:00-04:00") == "2026-06-30 12:00:00Z"
    assert telemetry.format_timestamp_utc("2026-06-30T12:00:00") == "2026-06-30 12:00:00Z"
    assert telemetry.format_timestamp_utc(None) == "?"
