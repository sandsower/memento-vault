"""Tests for capture-time dedup/merge via embedding."""

from __future__ import annotations

from pathlib import Path

from memento.dedup_merge import find_merge_target, merge_into_canonical
from memento.store import write_note
from memento.search_backend import SearchBackend


def _seed_note(vault: Path, *, title: str, body: str) -> str:
    result = write_note(
        vault,
        title=title,
        body=body,
        note_type="discovery",
        tags=["test"],
        certainty=3,
        source="test",
    )
    return str(result.relative_to(vault))


class _FakeBackend(SearchBackend):
    """A minimal SearchBackend that returns canned vector search results."""

    def __init__(self, results: list[dict] | None = None):
        self._results = results or []
        self._is_avail = True

    def is_available(self) -> bool:
        return self._is_avail

    def search(
        self,
        query: str,
        collection: str,
        limit: int = 5,
        semantic: bool = False,
        timeout: int = 10,
        min_score: float = 0.0,
        concrete: bool = False,
    ) -> list[dict]:
        return [r for r in self._results if r.get("score", 0) >= min_score][:limit]

    def reindex(self, collection: str, embed: bool = True) -> bool:
        return True

    def index_note(self, rel_path: str) -> bool:
        return True

    def repair_index(self, collection: str) -> dict:
        return {"reindexed": False, "repaired": 0, "errors": []}

    def get(self, path: str, collection: str | None = None, timeout: int = 5) -> dict | None:
        """Not used by dedup tests; return None."""
        return None

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# find_merge_target
# ---------------------------------------------------------------------------


def test_find_merge_target_above_threshold_returns_match(tmp_vault, monkeypatch):
    """A close embedding match above the threshold returns a MergeTarget."""
    # Simulate a backend hit: a FakeBackend that returns a match
    path = _seed_note(tmp_vault, title="Redis cache guidance", body="Use TTL on Redis keys.")
    fake_backend = _FakeBackend(results=[{"path": path, "title": "Redis cache guidance", "score": 0.95}])
    monkeypatch.setattr("memento.dedup_merge._is_embedding_backend", lambda _: True)
    monkeypatch.setattr("memento.dedup_merge._get_backend", lambda: fake_backend)
    monkeypatch.setattr("memento.dedup_merge.get_vault", lambda: tmp_vault)

    target = find_merge_target(
        "Redis cache TTL policy",
        "Set explicit TTL on Redis keys to prevent stale reads.",
        tags=["redis", "caching"],
        threshold=0.5,
    )

    assert target is not None
    assert target.path == path
    assert target.similarity >= 0.9


def test_find_merge_target_returns_none_for_low_similarity(tmp_vault, monkeypatch):
    """Below-threshold embedding returns None (creates new note)."""
    _seed_note(tmp_vault, title="Redis cache guidance", body="Use TTL on Redis keys.")
    _seed_note(tmp_vault, title="Docker compose networking", body="Bridge network for inter-service comms.")

    fake_backend = _FakeBackend(
        results=[{"path": "notes/docker-compose-networking.md", "title": "Docker compose networking", "score": 0.3}]
    )
    monkeypatch.setattr("memento.dedup_merge._is_embedding_backend", lambda _: True)
    monkeypatch.setattr("memento.dedup_merge._get_backend", lambda: fake_backend)
    monkeypatch.setattr("memento.dedup_merge.get_vault", lambda: tmp_vault)

    target = find_merge_target(
        "PostgreSQL index tuning",
        "Use partial indexes for filtered queries.",
        tags=["postgres"],
        threshold=0.5,
    )
    assert target is None


def test_find_merge_target_fallback_when_provider_unavailable(tmp_vault, monkeypatch):
    """When the embedding provider is unavailable, fall back to token overlap (no merge)."""
    _seed_note(tmp_vault, title="Redis cache guidance", body="Use TTL on Redis keys.")

    # _is_embedding_backend returns False -> find_merge_target short-circuits to None
    monkeypatch.setattr("memento.dedup_merge._is_embedding_backend", lambda _: False)
    monkeypatch.setattr("memento.dedup_merge.get_vault", lambda: tmp_vault)

    target = find_merge_target("Redis cache guidance", "Set explicit TTL on Redis keys.", threshold=0.86)
    assert target is None


# ---------------------------------------------------------------------------
# merge_into_canonical
# ---------------------------------------------------------------------------


def test_merge_appends_body_with_header(tmp_vault, monkeypatch):
    """merge_into_canonical appends incoming body under a Merged-from section."""
    monkeypatch.setattr("memento.dedup_merge.get_vault", lambda: tmp_vault)
    monkeypatch.setattr("memento.dedup_merge.load_access_log_stats", lambda: {})
    monkeypatch.setattr("memento.dedup_merge.write_access_log_stats", lambda _: None)
    monkeypatch.setattr("memento.dedup_merge._update_project_index", lambda *a, **kw: None)
    monkeypatch.setattr("memento.dedup_merge._index_note", lambda *a, **kw: None)

    path = _seed_note(tmp_vault, title="Redis cache guidance", body="Use TTL on Redis keys.")

    result = merge_into_canonical(
        tmp_vault,
        path,
        title="Redis TTL policy update",
        body="Set a 24-hour TTL for session caches.",
        note_type="discovery",
        tags=["redis", "ttl"],
        certainty=4,
    )

    assert result["canonical_path"] == path
    assert result["merged"] is True

    # Read the merged canonical note
    merged = (tmp_vault / path).read_text(encoding="utf-8")
    assert "Set a 24-hour TTL for session caches." in merged
    assert "## Merged from" in merged


def test_merge_unions_tags(tmp_vault, monkeypatch):
    """Merged note has the union of old and new tags."""
    monkeypatch.setattr("memento.dedup_merge.get_vault", lambda: tmp_vault)
    monkeypatch.setattr("memento.dedup_merge.load_access_log_stats", lambda: {})
    monkeypatch.setattr("memento.dedup_merge.write_access_log_stats", lambda _: None)
    monkeypatch.setattr("memento.dedup_merge._update_project_index", lambda *a, **kw: None)
    monkeypatch.setattr("memento.dedup_merge._index_note", lambda *a, **kw: None)

    path = _seed_note(tmp_vault, title="Redis cache", body="Use TTL.")
    merge_into_canonical(
        tmp_vault,
        path,
        title="Redis cache update",
        body="Additional guidance.",
        note_type="discovery",
        tags=["redis", "ttl"],
        certainty=3,
    )

    merged = (tmp_vault / path).read_text(encoding="utf-8")
    assert "redis" in merged
    assert "ttl" in merged


def test_merge_takes_max_certainty(tmp_vault, monkeypatch):
    """Merged note uses the higher certainty of the two."""
    monkeypatch.setattr("memento.dedup_merge.get_vault", lambda: tmp_vault)
    monkeypatch.setattr("memento.dedup_merge.load_access_log_stats", lambda: {})
    monkeypatch.setattr("memento.dedup_merge.write_access_log_stats", lambda _: None)
    monkeypatch.setattr("memento.dedup_merge._update_project_index", lambda *a, **kw: None)
    monkeypatch.setattr("memento.dedup_merge._index_note", lambda *a, **kw: None)

    path = _seed_note(tmp_vault, title="Redis cache", body="Use TTL.")  # certainty=3
    merge_into_canonical(
        tmp_vault,
        path,
        title="Redis cache update",
        body="More specific TTL guidance.",
        note_type="discovery",
        tags=["redis"],
        certainty=5,  # higher than existing 3
    )

    merged = (tmp_vault / path).read_text(encoding="utf-8")
    assert "certainty: 5" in merged


def test_merge_idempotent_no_double_append(tmp_vault, monkeypatch):
    """Re-merging the same payload does not add a duplicate section."""
    monkeypatch.setattr("memento.dedup_merge.get_vault", lambda: tmp_vault)
    monkeypatch.setattr("memento.dedup_merge.load_access_log_stats", lambda: {})
    monkeypatch.setattr("memento.dedup_merge.write_access_log_stats", lambda _: None)
    monkeypatch.setattr("memento.dedup_merge._update_project_index", lambda *a, **kw: None)
    monkeypatch.setattr("memento.dedup_merge._index_note", lambda *a, **kw: None)

    path = _seed_note(tmp_vault, title="Redis cache", body="Use TTL.")

    subject = {
        "title": "Redis TTL update",
        "body": "Set 24h TTL for session caches.",
        "note_type": "discovery",
        "tags": ["redis"],
        "certainty": 3,
    }

    result1 = merge_into_canonical(tmp_vault, path, **subject)
    assert result1["merged"] is True

    # Second merge of same content
    result2 = merge_into_canonical(tmp_vault, path, **subject)
    # Should be idempotent
    assert result2["merged"] is False
    assert "already_merged" in result2.get("reason", "")

    # Verify only one extra section in the body
    merged = (tmp_vault / path).read_text(encoding="utf-8")
    count = merged.count("## Merged from")
    assert count == 1, f"Expected 1 merged section, got {count}"


def test_access_log_survives_merge(tmp_vault, monkeypatch):
    """After merge, the existing access-log stats for the canonical path survive."""
    monkeypatch.setattr("memento.dedup_merge.get_vault", lambda: tmp_vault)
    monkeypatch.setattr("memento.dedup_merge._update_project_index", lambda *a, **kw: None)
    monkeypatch.setattr("memento.dedup_merge._index_note", lambda *a, **kw: None)

    path = _seed_note(tmp_vault, title="Redis cache", body="Use TTL.")

    # Pre-populate access log stats for the canonical
    initial_stats = {path: {"events": [{"ts": "2026-07-05T12:00:00", "rank": 1}]}}
    monkeypatch.setattr("memento.dedup_merge.load_access_log_stats", lambda: initial_stats)

    merge_into_canonical(
        tmp_vault,
        path,
        title="Redis TTL update",
        body="Set 24h TTL.",
        note_type="discovery",
        tags=["redis"],
        certainty=3,
    )

    # The merge does not touch the access log when old == canonical path
    # (standard case).  Stats should still be in their pre-populated state.
    assert path in initial_stats
    assert len(initial_stats[path]["events"]) == 1
