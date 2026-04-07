"""Tests for Inception LLM caller and response parser."""

import json
from unittest.mock import patch

from memento_inception import call_llm, parse_synthesis
from memento.llm import LLMResult


class TestParseSynthesis:
    """Tests for parse_synthesis."""

    def test_parse_valid_json(self):
        """Valid JSON with all fields returns dict with correct values."""
        raw = json.dumps(
            {
                "title": "Redis TTL patterns",
                "body": "Cross-project TTL strategy emerged.",
                "tags": ["redis", "caching"],
                "certainty": 4,
                "related": ["redis-cache-ttl", "redis-eviction-policy"],
            }
        )

        result = parse_synthesis(raw)

        assert result is not None
        assert result["title"] == "Redis TTL patterns"
        assert result["body"] == "Cross-project TTL strategy emerged."
        assert result["tags"] == ["redis", "caching"]
        assert result["certainty"] == 4
        assert result["related"] == ["redis-cache-ttl", "redis-eviction-policy"]

    def test_parse_skip(self):
        """Bare SKIP response returns None."""
        assert parse_synthesis("SKIP") is None

    def test_parse_skip_with_reason(self):
        """SKIP with a trailing reason returns None."""
        assert parse_synthesis("SKIP: trivial connection") is None

    def test_parse_empty(self):
        """Empty string returns None."""
        assert parse_synthesis("") is None

    def test_parse_malformed_json(self):
        """Non-JSON string returns None."""
        assert parse_synthesis("not json at all") is None

    def test_parse_missing_title(self):
        """JSON without title field returns None."""
        raw = json.dumps({"body": "some body", "tags": []})
        assert parse_synthesis(raw) is None

    def test_parse_code_fenced(self):
        """JSON wrapped in markdown code fences is correctly parsed."""
        inner = json.dumps(
            {
                "title": "Fenced pattern",
                "body": "Insight from fenced JSON.",
                "tags": ["meta"],
                "certainty": 3,
                "related": ["note-a"],
            }
        )
        raw = f"```json\n{inner}\n```"

        result = parse_synthesis(raw)

        assert result is not None
        assert result["title"] == "Fenced pattern"
        assert result["body"] == "Insight from fenced JSON."

    def test_parse_defaults(self):
        """JSON with only title and body gets default certainty=3, tags=[], related=[]."""
        raw = json.dumps({"title": "Minimal", "body": "Just the basics."})

        result = parse_synthesis(raw)

        assert result is not None
        assert result["certainty"] == 3
        assert result["tags"] == []
        assert result["related"] == []


class TestCallLlm:
    """Tests for call_llm via shared llm_complete."""

    @patch("memento_inception.llm_complete")
    def test_call_codex_command(self, mock_complete):
        mock_complete.return_value = LLMResult(text="some output", ok=True, error=None)

        result = call_llm("test prompt", {"inception_backend": "codex"})

        mock_complete.assert_called_once_with(
            "test prompt",
            {"llm_backend": "codex", "llm_model": None},
        )
        assert result == "some output"

    @patch("memento_inception.llm_complete")
    def test_call_claude_command(self, mock_complete):
        mock_complete.return_value = LLMResult(text="claude output", ok=True, error=None)

        result = call_llm(
            "test prompt",
            {
                "inception_backend": "claude",
                "inception_model": "sonnet",
            },
        )

        mock_complete.assert_called_once_with(
            "test prompt",
            {"llm_backend": "claude", "llm_model": "sonnet"},
        )
        assert result == "claude output"

    @patch("memento_inception.llm_complete")
    def test_call_timeout(self, mock_complete):
        mock_complete.return_value = LLMResult(text="", ok=False, error="timed out")

        result = call_llm("prompt", {"inception_backend": "codex"})

        assert result == ""

    @patch("memento_inception.llm_complete")
    def test_call_missing_binary(self, mock_complete):
        mock_complete.return_value = LLMResult(text="", ok=False, error="codex not found")

        result = call_llm("prompt", {"inception_backend": "codex"})

        assert result == ""
