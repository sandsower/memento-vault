"""Tests for concept index: build (Inception side) and lookup (Tenet side)."""

import pytest

from memento_inception import build_concept_index, write_concept_index
from memento.graph import load_concept_index, lookup_concepts


# --- build_concept_index ---


class TestBuildConceptIndex:
    def test_build_from_pattern_notes(self, tmp_vault, sample_notes):
        """Inception pattern notes should appear in the index keyed by tags, title words, and synthesized_from stems."""
        index = build_concept_index(tmp_vault)

        # "redis" comes from tags and synthesized_from stems
        assert "redis" in index
        stems = [entry["stem"] for entry in index["redis"]]
        assert "existing-pattern" in stems

        # "caching" comes from tags
        assert "caching" in index
        stems = [entry["stem"] for entry in index["caching"]]
        assert "existing-pattern" in stems

    def test_skips_non_inception(self, tmp_vault, sample_notes):
        """Regular notes (without source: inception) must NOT appear in the index."""
        index = build_concept_index(tmp_vault)

        # Collect all stems that appear anywhere in the index
        all_stems = set()
        for entries in index.values():
            for entry in entries:
                all_stems.add(entry["stem"])

        # Non-inception notes should be absent
        assert "redis-cache-ttl" not in all_stems
        assert "zustand-state-reset" not in all_stems
        assert "react-query-wrapper" not in all_stems

    def test_empty_vault(self, tmp_vault):
        """A vault with no inception notes produces an empty index."""
        index = build_concept_index(tmp_vault)
        assert index == {}


# --- write / load round-trip ---


class TestWriteAndLoadConceptIndex:
    def test_round_trip(self, tmp_vault, sample_notes, tmp_path):
        """Build → write → load should preserve the index data."""
        index = build_concept_index(tmp_vault)
        config_dir = str(tmp_path / "config")

        write_concept_index(index, config_dir=config_dir)
        loaded = load_concept_index(config_dir=config_dir)

        assert "redis" in loaded
        assert loaded["redis"] == index["redis"]

    def test_load_missing_file(self, tmp_path):
        """Loading from a nonexistent path returns an empty dict."""
        loaded = load_concept_index(config_dir=str(tmp_path / "nope"))
        assert loaded == {}


# --- lookup_concepts ---


class TestLookupConcepts:
    def _make_index(self):
        return {
            "redis": [
                {"stem": "pattern-a", "title": "Redis patterns", "score": 0.6},
            ],
            "caching": [
                {"stem": "pattern-a", "title": "Redis patterns", "score": 0.6},
                {"stem": "pattern-b", "title": "Cache layer design", "score": 0.8},
            ],
            "postgres": [
                {"stem": "pattern-c", "title": "Postgres indexing", "score": 0.7},
            ],
        }

    def test_single_keyword(self):
        """A single-keyword query returns matching patterns."""
        index = self._make_index()
        results = lookup_concepts("redis", index=index)

        assert len(results) >= 1
        assert results[0]["path"] == "notes/pattern-a.md"

    def test_multi_keyword_boost(self):
        """When multiple query words match the same pattern, scores are summed."""
        index = self._make_index()
        results = lookup_concepts("redis caching", index=index)

        # pattern-a matches both "redis" (0.6) and "caching" (0.6) → summed = 1.2
        # pattern-b matches only "caching" (0.8)
        assert results[0]["path"] == "notes/pattern-a.md"
        assert results[0]["score"] == pytest.approx(1.2)

    def test_no_match(self):
        """A query with no matching keywords returns an empty list."""
        index = self._make_index()
        results = lookup_concepts("kubernetes deployment", index=index)
        assert results == []

    def test_limit(self):
        """Even with many matches, return at most 5 results."""
        # Build an index with 10 distinct patterns under one keyword
        index = {
            "api": [{"stem": f"pattern-{i}", "title": f"Pattern {i}", "score": 0.5 + i * 0.01} for i in range(10)],
        }
        results = lookup_concepts("api", index=index)
        assert len(results) <= 5


# --- Concept index integration with recall hook merge logic ---


class TestConceptIndexIntegration:
    """Test the concept index merge logic used in vault-recall.py."""

    def _merge_concept_hits(self, bm25_results, concept_hits, config=None, min_score=0.4):
        """Replicate the merge logic from PromptRecallRuntime.run()
        (memento/retrieval_policy.py), including the MEM-141 min_score gate
        and post-merge re-sort.
        """
        if config is None:
            config = {}
        results = list(bm25_results)
        if config.get("concept_index_enabled", True):
            try:
                existing_paths = {r.get("path", "") for r in results}
                merged_any = False
                for hit in concept_hits:
                    if hit["path"] in existing_paths:
                        continue
                    hit["score"] = max(
                        hit.get("score", 0),
                        config.get("concept_index_score", 0.5),
                    )
                    if hit["score"] < min_score:
                        continue
                    results.append(hit)
                    existing_paths.add(hit["path"])
                    merged_any = True
                if merged_any:
                    results.sort(key=lambda r: r.get("score", 0), reverse=True)
            except Exception:
                pass
        return results

    def test_concept_hit_added_when_not_in_bm25(self):
        """Concept hit for a path not in BM25 results should be merged."""
        bm25_results = [
            {"path": "notes/redis-cache-ttl.md", "title": "Redis TTL", "score": 0.8},
        ]
        concept_hits = [
            {"path": "notes/existing-pattern.md", "title": "Cross-project Redis patterns", "score": 0.6},
        ]

        merged = self._merge_concept_hits(bm25_results, concept_hits)

        assert len(merged) == 2
        assert merged[1]["path"] == "notes/existing-pattern.md"
        assert merged[1]["title"] == "Cross-project Redis patterns"

    def test_concept_hit_deduped_when_in_bm25(self):
        """Concept hit for a path already in BM25 results should not duplicate."""
        bm25_results = [
            {"path": "notes/redis-cache-ttl.md", "title": "Redis TTL", "score": 0.8},
        ]
        concept_hits = [
            {"path": "notes/redis-cache-ttl.md", "title": "Redis TTL", "score": 0.5},
        ]

        merged = self._merge_concept_hits(bm25_results, concept_hits)

        assert len(merged) == 1
        assert merged[0]["path"] == "notes/redis-cache-ttl.md"

    def test_concept_index_disabled(self):
        """When concept_index_enabled=False, no concept lookup should occur."""
        bm25_results = [
            {"path": "notes/redis-cache-ttl.md", "title": "Redis TTL", "score": 0.8},
        ]
        concept_hits = [
            {"path": "notes/new-pattern.md", "title": "New pattern", "score": 0.7},
        ]

        merged = self._merge_concept_hits(bm25_results, concept_hits, config={"concept_index_enabled": False})

        assert len(merged) == 1
        assert merged[0]["path"] == "notes/redis-cache-ttl.md"

    def test_concept_score_floor_applied(self):
        """Concept hits with low scores get bumped to the configured floor."""
        bm25_results = []
        concept_hits = [
            {"path": "notes/low-score.md", "title": "Low score", "score": 0.1},
        ]

        merged = self._merge_concept_hits(bm25_results, concept_hits)

        # Default floor is 0.5
        assert merged[0]["score"] == 0.5

    def test_concept_score_floor_custom(self):
        """Custom concept_index_score config overrides the default floor."""
        bm25_results = []
        concept_hits = [
            {"path": "notes/low-score.md", "title": "Low score", "score": 0.1},
        ]

        merged = self._merge_concept_hits(bm25_results, concept_hits, config={"concept_index_score": 0.7})

        assert merged[0]["score"] == 0.7

    def test_concept_score_above_floor_preserved(self):
        """Concept hits with scores above the floor keep their original score."""
        bm25_results = []
        concept_hits = [
            {"path": "notes/high-score.md", "title": "High score", "score": 0.9},
        ]

        merged = self._merge_concept_hits(bm25_results, concept_hits)

        assert merged[0]["score"] == 0.9

    def test_concept_hit_filtered_by_min_score(self):
        """A concept hit whose floored score still trails recall_min_score is dropped (MEM-141)."""
        bm25_results = []
        concept_hits = [
            {"path": "notes/low-score.md", "title": "Low score", "score": 0.1},
        ]

        # Default floor (0.5) clears a stricter min_score threshold's bar only
        # if the threshold itself is <= the floor -- here it isn't.
        merged = self._merge_concept_hits(bm25_results, concept_hits, min_score=0.6)

        assert merged == []

    def test_concept_hit_admitted_when_floor_clears_min_score(self):
        """A concept hit whose floored score clears recall_min_score is still merged."""
        bm25_results = []
        concept_hits = [
            {"path": "notes/low-score.md", "title": "Low score", "score": 0.1},
        ]

        merged = self._merge_concept_hits(bm25_results, concept_hits, min_score=0.4)

        assert len(merged) == 1
        assert merged[0]["score"] == 0.5

    def test_concept_hits_resorted_after_merge(self):
        """Concept hits are merged by score order, not by lookup/insertion order (MEM-141).

        A weak BM25 candidate must not out-rank a stronger concept-index hit
        just because it was appended first.
        """
        bm25_results = [
            {"path": "notes/weak-bm25.md", "title": "Weak BM25 hit", "score": 0.45},
        ]
        concept_hits = [
            {"path": "notes/strong-concept.md", "title": "Strong concept hit", "score": 0.9},
        ]

        merged = self._merge_concept_hits(bm25_results, concept_hits)

        assert [r["path"] for r in merged] == ["notes/strong-concept.md", "notes/weak-bm25.md"]

    def test_multiple_novel_concept_hits(self):
        """Multiple novel concept hits are all merged, duplicates excluded."""
        bm25_results = [
            {"path": "notes/redis-cache-ttl.md", "title": "Redis TTL", "score": 0.8},
        ]
        concept_hits = [
            {"path": "notes/pattern-a.md", "title": "Pattern A", "score": 0.6},
            {"path": "notes/redis-cache-ttl.md", "title": "Redis TTL", "score": 0.5},
            {"path": "notes/pattern-b.md", "title": "Pattern B", "score": 0.7},
        ]

        merged = self._merge_concept_hits(bm25_results, concept_hits)

        assert len(merged) == 3
        paths = [r["path"] for r in merged]
        assert "notes/pattern-a.md" in paths
        assert "notes/pattern-b.md" in paths
        # The duplicate should not appear twice
        assert paths.count("notes/redis-cache-ttl.md") == 1
