"""Tests for multi-hop retrieval gate and search."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))

from memento_utils import needs_multi_hop, multi_hop_search


class TestNeedsMultiHop:
    """Gate function should detect prompts needing chained retrieval."""

    def test_temporal_last_time(self):
        assert needs_multi_hop("What did we decide last time about the cache?") is True

    def test_temporal_before(self):
        assert needs_multi_hop("Before we changed the auth flow, what was the approach?") is True

    def test_temporal_previous(self):
        assert needs_multi_hop("What was the previous implementation of the API?") is True

    def test_temporal_when_did(self):
        assert needs_multi_hop("When did we switch from REST to GraphQL?") is True

    def test_temporal_changed(self):
        assert needs_multi_hop("What changed about the billing module since last week?") is True

    def test_temporal_used_to(self):
        assert needs_multi_hop("The caching layer used to work differently, what happened?") is True

    def test_cross_ref_same_as(self):
        assert needs_multi_hop("Is this the same approach as the billing project?") is True

    def test_cross_ref_like_we_did(self):
        assert needs_multi_hop("Can we do it like we did in the auth service?") is True

    def test_cross_ref_other_project(self):
        assert needs_multi_hop("How did we handle this in that other project?") is True

    def test_normal_prompt_no_hop(self):
        assert needs_multi_hop("Fix the broken test in auth.py") is False

    def test_simple_question_no_hop(self):
        assert needs_multi_hop("How does the cache layer work?") is False

    def test_short_prompt_no_hop(self):
        assert needs_multi_hop("run tests") is False

    def test_empty_prompt(self):
        assert needs_multi_hop("") is False

    def test_code_prompt_no_hop(self):
        assert needs_multi_hop("Add a retry mechanism to the HTTP client") is False

    def test_case_insensitive(self):
        assert needs_multi_hop("LAST TIME we tried this it broke") is True

    def test_across_projects(self):
        assert needs_multi_hop("We solved this same problem across three repos") is True


class TestMultiHopSearch:
    """Multi-hop should extract entities from initial results and search again."""

    def _make_result(self, path, title, snippet, score=0.5):
        return {"path": path, "title": title, "snippet": snippet, "score": score}

    def test_extracts_entities_and_searches(self):
        initial = [
            self._make_result("notes/a.md", "Redis cache TTL", "The Redis cluster at Production East uses allkeys-lru eviction.", 0.6),
            self._make_result("notes/b.md", "API cache", "Cache invalidation for the Billing API endpoint.", 0.5),
        ]

        mock_followup = [
            self._make_result("notes/c.md", "Production East config", "Server config for Production East region.", 0.55),
        ]

        with patch("memento_utils.qmd_search_with_extras", return_value=mock_followup):
            results = multi_hop_search("how does caching work", initial, config={"multi_hop_max": 1})

        # Should have initial + new results
        assert len(results) == 3
        paths = [r["path"] for r in results]
        assert "notes/c.md" in paths

    def test_deduplicates_by_path(self):
        initial = [
            self._make_result("notes/a.md", "Redis", "The Redis cluster config.", 0.6),
        ]

        # Follow-up returns the same note
        mock_followup = [
            self._make_result("notes/a.md", "Redis", "The Redis cluster config.", 0.55),
        ]

        with patch("memento_utils.qmd_search_with_extras", return_value=mock_followup):
            results = multi_hop_search("redis config", initial, config={"multi_hop_max": 1})

        assert len(results) == 1

    def test_no_entities_no_search(self):
        initial = [
            self._make_result("notes/a.md", "config", "some lowercase text with no names.", 0.5),
        ]

        with patch("memento_utils.qmd_search_with_extras") as mock_search:
            results = multi_hop_search("how does config work", initial, config={"multi_hop_max": 1})

        # Should not have called follow-up search
        mock_search.assert_not_called()
        assert len(results) == 1

    def test_respects_max_hops(self):
        initial = [
            self._make_result("notes/a.md", "Redis", "Production East cluster.", 0.6),
        ]

        call_count = 0
        def mock_search(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return [self._make_result(f"notes/hop{call_count}.md", f"Hop {call_count}", f"Result from hop {call_count}. Production West too.", 0.5)]

        with patch("memento_utils.qmd_search_with_extras", side_effect=mock_search):
            results = multi_hop_search("redis clusters", initial, config={"multi_hop_max": 2})

        assert call_count <= 2

    def test_empty_initial_results(self):
        with patch("memento_utils.qmd_search_with_extras") as mock_search:
            results = multi_hop_search("something", [], config={"multi_hop_max": 1})

        mock_search.assert_not_called()
        assert results == []

    def test_sorted_by_score(self):
        initial = [
            self._make_result("notes/a.md", "Low score", "Some text with Boston.", 0.3),
        ]

        mock_followup = [
            self._make_result("notes/b.md", "High score", "Better result.", 0.8),
        ]

        with patch("memento_utils.qmd_search_with_extras", return_value=mock_followup):
            results = multi_hop_search("test", initial, config={"multi_hop_max": 1})

        assert results[0]["path"] == "notes/b.md"
