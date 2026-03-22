"""Tests for LongMemEval BM25 retrieval layer."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "benchmark"))

import pytest
from longmemeval_retrieval import BM25Index, tokenize, build_bm25_index, bm25_search


# ---- fixtures ----

DOCS_FIVE = [
    {"id": "session-1", "text": "We discussed the quarterly budget review and revenue targets."},
    {"id": "session-2", "text": "Alice mentioned her vacation plans for July in Greece."},
    {"id": "session-3", "text": "The deployment pipeline broke due to a flaky integration test."},
    {"id": "session-4", "text": "Team standup: sprint velocity is trending upward this cycle."},
    {
        "id": "session-5",
        "text": "Design review for the new onboarding flow with updated illustrations.",
        "metadata": {"title": "Design Review", "date": "2025-11-10"},
    },
]

DOCS_THREE_TOPICS = [
    {"id": "cooking", "text": "The recipe calls for garlic, olive oil, and fresh basil. Simmer the tomato sauce for thirty minutes."},
    {"id": "astronomy", "text": "The telescope captured images of Jupiter's moons. Ganymede is the largest moon in the solar system."},
    {"id": "gardening", "text": "Plant the tomatoes in full sun. Water deeply once a week and mulch to retain moisture."},
]


# ---- tokenize ----

class TestTokenize:
    def test_tokenize_basic(self):
        """Lowercase, split on non-alnum, keep tokens len>1."""
        result = tokenize("Hello World! Test-case")
        assert result == ["hello", "world", "test", "case"]

    def test_tokenize_filters_short(self):
        """Single-char tokens are dropped."""
        result = tokenize("I a am OK x y go")
        # 'I', 'a', 'x', 'y' are single-char -> dropped
        assert "i" not in result
        assert "a" not in result
        assert "x" not in result
        assert "y" not in result
        assert "am" in result
        assert "ok" in result
        assert "go" in result


# ---- build_bm25_index ----

class TestBuildBm25Index:
    def test_build_bm25_index(self):
        """Build index from 5 docs, returns BM25Index with correct fields."""
        idx = build_bm25_index(DOCS_FIVE)
        assert isinstance(idx, BM25Index)
        assert len(idx.documents) == 5
        assert len(idx.corpus_tokens) == 5
        assert idx.bm25 is not None


# ---- bm25_search ----

class TestBm25Search:
    def test_bm25_search_basic(self):
        """Searching for 'telescope Jupiter moons' should rank astronomy doc first."""
        idx = build_bm25_index(DOCS_THREE_TOPICS)
        results = bm25_search(idx, "telescope Jupiter moons")
        assert len(results) >= 1
        assert results[0]["_doc_id"] == "astronomy"

    def test_bm25_search_respects_limit(self):
        """Limit caps the number of results returned."""
        idx = build_bm25_index(DOCS_THREE_TOPICS)
        results = bm25_search(idx, "tomato", limit=2)
        assert len(results) <= 2

    def test_bm25_search_min_score_filter(self):
        """High min_score excludes low-scoring results."""
        idx = build_bm25_index(DOCS_THREE_TOPICS)
        # Use a very high min_score so nothing (or very little) passes
        results = bm25_search(idx, "telescope Jupiter moons", min_score=999.0)
        assert len(results) == 0

    def test_result_format_matches_memento(self):
        """Each result dict has the keys memento_utils expects."""
        idx = build_bm25_index(DOCS_THREE_TOPICS)
        results = bm25_search(idx, "garlic olive oil basil")
        assert len(results) >= 1
        required_keys = {"path", "title", "score", "snippet"}
        for r in results:
            assert required_keys.issubset(r.keys()), f"Missing keys in {r.keys()}"

    def test_bm25_search_empty_query(self):
        """Empty query string returns empty list."""
        idx = build_bm25_index(DOCS_THREE_TOPICS)
        assert bm25_search(idx, "") == []

    def test_bm25_search_no_matches(self):
        """Query with no matching terms returns empty (below default min_score)."""
        idx = build_bm25_index(DOCS_THREE_TOPICS)
        results = bm25_search(idx, "xylophone zeppelin", min_score=0.01)
        assert len(results) == 0

    def test_score_normalization(self):
        """All returned scores should be in [0, 1] range."""
        idx = build_bm25_index(DOCS_FIVE)
        results = bm25_search(idx, "budget revenue quarterly review")
        for r in results:
            assert 0.0 <= r["score"] <= 1.0, f"Score {r['score']} out of range"

    def test_path_format(self):
        """Path follows notes/<id>.md convention."""
        idx = build_bm25_index(DOCS_THREE_TOPICS)
        results = bm25_search(idx, "garlic basil")
        assert len(results) >= 1
        assert results[0]["path"] == "notes/cooking.md"

    def test_title_from_metadata(self):
        """Title is pulled from metadata when present."""
        idx = build_bm25_index(DOCS_FIVE)
        results = bm25_search(idx, "onboarding design illustrations")
        assert len(results) >= 1
        design_results = [r for r in results if r["_doc_id"] == "session-5"]
        assert len(design_results) == 1
        assert design_results[0]["title"] == "Design Review"
