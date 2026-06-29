"""Tests for derived access-log retrieval boosts."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from memento import store
from memento.mcp_server import memento_get
from memento.store import apply_access_log_boost, record_access


def test_record_access_writes_derived_jsonl_without_touching_notes(tmp_vault):
    note_path = tmp_vault / "notes" / "example.md"
    note_path.write_text("---\ntitle: Example\n---\n\nbody\n")
    before = note_path.read_text()

    record_access(
        ["notes/example.md"], hook="mcp", tool="search", query="redis cache ttl", session_id="sess-1", result_count=1
    )

    assert note_path.read_text() == before

    log_path = Path(store.ACCESS_LOG_PATH)
    assert log_path.exists()
    assert tmp_vault not in log_path.parents

    entry = json.loads(log_path.read_text().strip())
    assert entry["path"] == "notes/example.md"
    assert entry["hook"] == "mcp"
    assert entry["tool"] == "search"
    assert entry["query_summary"] == "redis cache ttl"
    assert len(entry["query_hash"]) == 64


def test_apply_access_log_boost_prefers_recent_frequent_hits(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    log_path = Path(store.ACCESS_LOG_PATH)
    log_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": (now - timedelta(days=1)).isoformat(),
                        "path": "notes/fresh.md",
                        "hook": "mcp",
                        "tool": "search",
                        "rank": 1,
                    }
                ),
                json.dumps({"ts": now.isoformat(), "path": "notes/fresh.md", "hook": "mcp", "tool": "get", "rank": 1}),
                json.dumps(
                    {
                        "ts": (now - timedelta(days=2)).isoformat(),
                        "path": "notes/fresh.md",
                        "hook": "recall",
                        "tool": "inject",
                        "rank": 1,
                    }
                ),
                json.dumps(
                    {
                        "ts": (now - timedelta(days=90)).isoformat(),
                        "path": "notes/stale.md",
                        "hook": "mcp",
                        "tool": "search",
                        "rank": 1,
                    }
                ),
            ]
        )
        + "\n"
    )

    results = [
        {"path": "notes/fresh.md", "score": 1.0},
        {"path": "notes/stale.md", "score": 1.0},
        {"path": "notes/untouched.md", "score": 1.0},
    ]

    boosted = apply_access_log_boost(
        results,
        config={"access_log_enabled": True, "access_log_boost_weight": 0.2, "access_log_half_life_days": 30},
        now=now,
    )

    assert boosted[0]["path"] == "notes/fresh.md"
    assert boosted[0]["score"] > boosted[1]["score"] > boosted[2]["score"]


def test_access_log_boost_can_be_disabled(monkeypatch):
    monkeypatch.setattr("memento.store.get_config", lambda: {"access_log_enabled": False}, raising=False)

    record_access(["notes/disabled.md"], hook="mcp", tool="search", query="disabled boost", session_id="sess-2")
    assert not Path(store.ACCESS_LOG_PATH).exists()

    results = [{"path": "notes/disabled.md", "score": 1.0}]
    boosted = apply_access_log_boost(results, config={"access_log_enabled": False})
    assert boosted == results


def test_memento_get_records_access(tmp_vault, monkeypatch):
    note_path = tmp_vault / "notes" / "example.md"
    note_path.write_text("---\ntitle: Example\n---\n\nbody\n")
    monkeypatch.setattr("memento.mcp_server.get_vault", lambda: tmp_vault, raising=False)

    result = memento_get("notes/example.md")

    assert result["title"] == "Example"
    entry = json.loads(Path(store.ACCESS_LOG_PATH).read_text().strip())
    assert entry["path"] == "notes/example.md"
    assert entry["tool"] == "get"
