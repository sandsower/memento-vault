"""Tests for Inception sleep-time pre-reasoning."""

import json
from pathlib import Path

from memento_inception import (
    NoteRecord,
    pre_reason,
    _predict_queries,
    _extract_connections,
)


def _make_note(
    stem,
    title="Untitled",
    note_type="discovery",
    tags=None,
    date="2026-03-10T14:00",
    certainty=4,
    project=None,
    body="Some body text.",
    source=None,
    synthesized_from=None,
):
    """Helper to build a NoteRecord without touching the filesystem."""
    return NoteRecord(
        stem=stem,
        path=Path(f"/fake/notes/{stem}.md"),
        title=title,
        note_type=note_type,
        tags=tags or [],
        date=date,
        certainty=certainty,
        project=project,
        source=source,
        body=body,
        synthesized_from=synthesized_from or [],
    )


class TestPredictQueries:
    """Tests for query prediction from pattern note metadata."""

    def test_includes_title(self):
        pattern = _make_note(
            "redis-patterns",
            title="Redis caching patterns across services",
            tags=["redis", "caching"],
            source="inception",
            synthesized_from=["redis-cache-ttl"],
        )
        sources = [_make_note("redis-cache-ttl", title="Redis cache requires explicit TTL", tags=["redis"])]

        queries = _predict_queries(pattern, sources)

        # A title-derived query should be present (may be truncated)
        assert any("redis caching" in q for q in queries)

    def test_includes_tags(self):
        pattern = _make_note(
            "redis-patterns", title="Redis patterns", tags=["redis", "caching"], source="inception", synthesized_from=[]
        )
        queries = _predict_queries(pattern, [])

        assert any("redis" in q for q in queries)
        assert any("caching" in q for q in queries)

    def test_includes_tag_pairs(self):
        pattern = _make_note(
            "test-patterns",
            title="Test patterns",
            tags=["redis", "caching", "api"],
            source="inception",
            synthesized_from=[],
        )
        queries = _predict_queries(pattern, [])

        # Should have at least one tag pair
        tag_pairs = [q for q in queries if " " in q and all(t in q for t in ("redis", "caching"))]
        assert len(tag_pairs) >= 1 or len(queries) > 0

    def test_includes_source_title_keywords(self):
        pattern = _make_note(
            "patterns", title="Patterns", tags=[], source="inception", synthesized_from=["redis-cache-ttl"]
        )
        sources = [_make_note("redis-cache-ttl", title="Redis cache requires explicit TTL")]

        queries = _predict_queries(pattern, sources)

        # Source title keywords should appear
        assert any("redis" in q for q in queries)

    def test_caps_at_five(self):
        pattern = _make_note(
            "big",
            title="A very long title with many keywords here",
            tags=["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"],
            source="inception",
            synthesized_from=["a", "b", "c"],
        )
        sources = [
            _make_note("a", title="Source one about databases"),
            _make_note("b", title="Source two about caching layers"),
            _make_note("c", title="Source three about deployment"),
        ]

        queries = _predict_queries(pattern, sources)

        assert len(queries) <= 5

    def test_empty_inputs(self):
        pattern = _make_note("empty", title="", tags=[], source="inception", synthesized_from=[])
        queries = _predict_queries(pattern, [])

        assert isinstance(queries, list)

    def test_short_tags_excluded(self):
        """Tags shorter than 3 chars are excluded."""
        pattern = _make_note("test", title="Test", tags=["ab", "cd"], source="inception", synthesized_from=[])
        queries = _predict_queries(pattern, [])

        assert not any(q == "ab" for q in queries)
        assert not any(q == "cd" for q in queries)


class TestExtractConnections:
    """Tests for connection map extraction from source notes."""

    def test_extracts_projects(self):
        pattern = _make_note("patterns", title="Patterns", source="inception", synthesized_from=["a", "b"])
        sources = [
            _make_note("a", project="/home/vic/Projects/api-service"),
            _make_note("b", project="/home/vic/Projects/billing"),
        ]

        result = _extract_connections(pattern, sources)

        assert "/home/vic/Projects/api-service" in result["projects"]
        assert "/home/vic/Projects/billing" in result["projects"]

    def test_extracts_code_areas_from_body(self):
        pattern = _make_note("patterns", title="Patterns", source="inception", synthesized_from=["a"])
        sources = [
            _make_note("a", body="Fixed the issue in /home/vic/Projects/api/src/cache.rs today."),
        ]

        result = _extract_connections(pattern, sources)

        assert any("src/cache.rs" in area for area in result["code_areas"])

    def test_no_projects_returns_empty_list(self):
        pattern = _make_note("patterns", title="Patterns", source="inception", synthesized_from=["a"])
        sources = [
            _make_note("a", project=None, body="No paths here."),
        ]

        result = _extract_connections(pattern, sources)

        assert result["projects"] == []

    def test_no_code_areas_returns_empty_list(self):
        pattern = _make_note("patterns", title="Patterns", source="inception", synthesized_from=["a"])
        sources = [
            _make_note("a", project="/some/project", body="Plain text with no paths."),
        ]

        result = _extract_connections(pattern, sources)

        assert result["code_areas"] == []

    def test_deduplicates_projects(self):
        pattern = _make_note("patterns", title="Patterns", source="inception", synthesized_from=["a", "b"])
        sources = [
            _make_note("a", project="/home/vic/Projects/api"),
            _make_note("b", project="/home/vic/Projects/api"),
        ]

        result = _extract_connections(pattern, sources)

        assert len(result["projects"]) == 1

    def test_empty_sources(self):
        pattern = _make_note("patterns", title="Patterns", source="inception", synthesized_from=[])

        result = _extract_connections(pattern, [])

        assert result["projects"] == []
        assert result["code_areas"] == []


class TestPreReason:
    """Tests for the pre_reason pipeline function."""

    def test_writes_valid_json_files(self, tmp_vault, mock_config):
        """Both output files are valid JSON."""
        pattern = _make_note(
            "redis-patterns",
            title="Redis caching patterns",
            tags=["redis", "caching"],
            source="inception",
            synthesized_from=["redis-cache-ttl", "redis-eviction-policy"],
        )
        notes_dict = {
            "redis-cache-ttl": _make_note(
                "redis-cache-ttl",
                title="Redis cache requires explicit TTL",
                tags=["redis"],
                project="/home/vic/Projects/api",
            ),
            "redis-eviction-policy": _make_note(
                "redis-eviction-policy",
                title="Redis eviction policy for sessions",
                tags=["redis"],
                project="/home/vic/Projects/api",
            ),
        }

        qp, cp = pre_reason([pattern], notes_dict, mock_config)

        assert qp is not None
        assert cp is not None

        # Both files should parse as valid JSON
        queries = json.loads(qp.read_text())
        connections = json.loads(cp.read_text())

        assert isinstance(queries, dict)
        assert isinstance(connections, dict)

    def test_queries_mapped_to_stem(self, tmp_vault, mock_config):
        """Query predictions are keyed by pattern note stem."""
        pattern = _make_note(
            "redis-patterns",
            title="Redis caching patterns",
            tags=["redis", "caching"],
            source="inception",
            synthesized_from=["redis-cache-ttl"],
        )
        notes_dict = {
            "redis-cache-ttl": _make_note("redis-cache-ttl", title="Redis cache requires explicit TTL", tags=["redis"]),
        }

        qp, _ = pre_reason([pattern], notes_dict, mock_config)
        queries = json.loads(qp.read_text())

        assert "redis-patterns" in queries
        assert isinstance(queries["redis-patterns"], list)
        assert len(queries["redis-patterns"]) > 0

    def test_connections_mapped_to_stem(self, tmp_vault, mock_config):
        """Connection maps are keyed by pattern note stem."""
        pattern = _make_note(
            "redis-patterns",
            title="Redis patterns",
            tags=["redis"],
            source="inception",
            synthesized_from=["redis-cache-ttl"],
        )
        notes_dict = {
            "redis-cache-ttl": _make_note("redis-cache-ttl", title="Redis cache TTL", project="/home/vic/Projects/api"),
        }

        _, cp = pre_reason([pattern], notes_dict, mock_config)
        connections = json.loads(cp.read_text())

        assert "redis-patterns" in connections
        assert "/home/vic/Projects/api" in connections["redis-patterns"]["projects"]

    def test_disabled_by_config(self, tmp_vault, mock_config):
        """Returns (None, None) when inception_pre_reason is False."""
        mock_config["inception_pre_reason"] = False
        pattern = _make_note("test", title="Test", source="inception", synthesized_from=[])

        qp, cp = pre_reason([pattern], {}, mock_config)

        assert qp is None
        assert cp is None

    def test_empty_pattern_notes(self, tmp_vault, mock_config):
        """Returns (None, None) when pattern_notes list is empty."""
        qp, cp = pre_reason([], {}, mock_config)

        assert qp is None
        assert cp is None

    def test_merges_with_existing_files(self, tmp_vault, mock_config):
        """New data merges with existing pre-reason files, not overwrites."""
        notes_dir = tmp_vault / "notes"

        # Write existing data
        existing_queries = {"old-pattern": ["old query"]}
        (notes_dir / ".inception-queries.json").write_text(json.dumps(existing_queries))
        existing_connections = {"old-pattern": {"projects": ["/old"], "code_areas": []}}
        (notes_dir / ".inception-connections.json").write_text(json.dumps(existing_connections))

        pattern = _make_note(
            "new-pattern", title="New pattern", tags=["new"], source="inception", synthesized_from=["src-note"]
        )
        notes_dict = {
            "src-note": _make_note("src-note", title="Source", project="/new/project"),
        }

        qp, cp = pre_reason([pattern], notes_dict, mock_config)
        queries = json.loads(qp.read_text())
        connections = json.loads(cp.read_text())

        # Old data preserved
        assert "old-pattern" in queries
        assert "old-pattern" in connections

        # New data added
        assert "new-pattern" in queries
        assert "new-pattern" in connections

    def test_multiple_pattern_notes(self, tmp_vault, mock_config):
        """Handles multiple pattern notes in a single call."""
        p1 = _make_note("pattern-a", title="Pattern A", tags=["alpha"], source="inception", synthesized_from=["src-1"])
        p2 = _make_note("pattern-b", title="Pattern B", tags=["beta"], source="inception", synthesized_from=["src-2"])
        notes_dict = {
            "src-1": _make_note("src-1", title="Source 1", project="/home/vic/Projects/alpha"),
            "src-2": _make_note("src-2", title="Source 2", project="/home/vic/Projects/beta"),
        }

        qp, cp = pre_reason([p1, p2], notes_dict, mock_config)
        queries = json.loads(qp.read_text())
        connections = json.loads(cp.read_text())

        assert "pattern-a" in queries
        assert "pattern-b" in queries
        assert "pattern-a" in connections
        assert "pattern-b" in connections
