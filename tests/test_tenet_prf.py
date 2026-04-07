"""Tests for PRF (Pseudo-Relevance Feedback) query expansion."""

from unittest.mock import patch

from memento_utils import _extract_expansion_terms, prf_expand_query, DEFAULT_CONFIG


# --- _extract_expansion_terms ---


class TestExtractExpansionTerms:
    def test_extract_terms_basic(self):
        """Given results about redis cache TTL, extracts relevant terms but not the original query."""
        results = [
            {"snippet": "Redis cache requires explicit TTL settings", "title": "Redis cache TTL"},
            {"snippet": "Cache invalidation with TTL expiry", "title": "Cache invalidation"},
            {"snippet": "Setting cache timeout for Redis keys", "title": "Redis timeout"},
        ]
        terms = _extract_expansion_terms(results, "redis")
        assert "redis" not in terms
        assert "cache" in terms
        assert "ttl" in terms
        assert len(terms) <= 5

    def test_extract_terms_filters_stopwords(self):
        """Stopwords like 'the', 'is', 'very' should not appear in results."""
        results = [
            {"snippet": "the redis cache is very fast and efficient", "title": "Redis perf"},
        ]
        terms = _extract_expansion_terms(results, "redis")
        for stop in ("the", "is", "very", "and"):
            assert stop not in terms

    def test_extract_terms_filters_short_words(self):
        """Words shorter than 3 characters should not appear."""
        results = [
            {"snippet": "an OK db to use as a cache layer", "title": "DB cache"},
        ]
        terms = _extract_expansion_terms(results, "query")
        for term in terms:
            assert len(term) >= 3

    def test_extract_terms_respects_max(self):
        """With max_terms=2, only return 2 terms."""
        results = [
            {"snippet": "cache invalidation timeout expiry latency", "title": "Cache stuff"},
        ]
        terms = _extract_expansion_terms(results, "query", max_terms=2)
        assert len(terms) <= 2

    def test_extract_terms_empty_results(self):
        """Empty results list returns empty list."""
        terms = _extract_expansion_terms([], "redis")
        assert terms == []

    def test_extract_terms_deduplicates(self):
        """Same term in multiple snippets counted for frequency but not duplicated in output."""
        results = [
            {"snippet": "cache cache cache", "title": "Cache note"},
            {"snippet": "cache invalidation", "title": "Another cache note"},
        ]
        terms = _extract_expansion_terms(results, "query")
        assert terms.count("cache") == 1


# --- prf_expand_query ---


class TestPrfExpandQuery:
    @patch("memento_utils.qmd_search")
    def test_prf_expands_query(self, mock_search):
        """Expanded query contains original query plus expansion terms."""
        mock_search.return_value = [
            {"snippet": "Redis cache requires explicit TTL settings", "title": "Redis cache TTL"},
            {"snippet": "Cache invalidation with TTL expiry", "title": "Cache invalidation"},
            {"snippet": "Setting cache timeout for Redis keys", "title": "Redis timeout"},
        ]
        config = dict(DEFAULT_CONFIG)
        config["prf_enabled"] = True

        result = prf_expand_query("redis", config=config)

        assert result.startswith("redis")
        assert len(result) > len("redis")
        mock_search.assert_called_once()

    @patch("memento_utils.qmd_search")
    def test_prf_no_results_returns_original(self, mock_search):
        """When search returns nothing, return original query unchanged."""
        mock_search.return_value = []
        config = dict(DEFAULT_CONFIG)
        config["prf_enabled"] = True

        result = prf_expand_query("obscure_query", config=config)

        assert result == "obscure_query"

    def test_prf_disabled_returns_original(self):
        """When prf_enabled=False, return original query without searching."""
        config = dict(DEFAULT_CONFIG)
        config["prf_enabled"] = False

        with patch("memento_utils.qmd_search") as mock_search:
            result = prf_expand_query("redis", config=config)

        assert result == "redis"
        mock_search.assert_not_called()

    @patch("memento_utils.qmd_search")
    def test_prf_preserves_original_query(self, mock_search):
        """The expanded query must START with the original query."""
        mock_search.return_value = [
            {"snippet": "cache timeout expiry invalidation", "title": "Cache note"},
        ]
        config = dict(DEFAULT_CONFIG)
        config["prf_enabled"] = True

        original = "my specific query"
        result = prf_expand_query(original, config=config)

        assert result.startswith(original)
