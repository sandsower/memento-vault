"""Tests for secret sanitization and prompt injection stripping."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))

from memento.utils import sanitize_secrets


class TestSanitizeSecrets:
    def test_openai_key(self):
        text = "Set OPENAI_API_KEY=sk-abc123def456ghi789jkl012mno345pqr678stu901vwx"
        result = sanitize_secrets(text)
        assert "sk-abc123" not in result
        assert "[REDACTED" in result

    def test_openai_project_key(self):
        text = "key is sk-proj-abc123_def456-ghi789_jkl012mno345"
        result = sanitize_secrets(text)
        assert "sk-proj-" not in result

    def test_github_pat(self):
        text = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
        result = sanitize_secrets(text)
        assert "ghp_" not in result
        assert "[REDACTED_GITHUB_TOKEN]" in result

    def test_github_new_pat(self):
        text = "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        result = sanitize_secrets(text)
        assert "github_pat_" not in result

    def test_slack_bot_token(self):
        text = "SLACK_TOKEN=xoxb-123456789012-abcdefghijkl"
        result = sanitize_secrets(text)
        assert "xoxb-" not in result

    def test_aws_access_key(self):
        text = "AWS key is AKIAIOSFODNN7EXAMPLE"
        result = sanitize_secrets(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_jwt_token(self):
        text = "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        result = sanitize_secrets(text)
        assert "eyJhbGciOiJ" not in result

    def test_postgres_url(self):
        text = "DATABASE_URL=postgres://admin:s3cret@db.example.com:5432/mydb"
        result = sanitize_secrets(text)
        assert "s3cret" not in result
        assert "[REDACTED_CONNECTION_STRING]" in result

    def test_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdef"
        result = sanitize_secrets(text)
        assert "Bearer [REDACTED" in result

    def test_env_var_secret(self):
        text = "API_SECRET=verylongsecretvalue1234567890abcdef"
        result = sanitize_secrets(text)
        assert "verylongsecret" not in result

    def test_preserves_normal_text(self):
        text = "The Redis cache requires explicit TTL settings for all keys."
        result = sanitize_secrets(text)
        assert result == text

    def test_none_input(self):
        assert sanitize_secrets(None) is None

    def test_empty_string(self):
        assert sanitize_secrets("") == ""

    def test_short_values_not_redacted(self):
        """Short values that happen to follow a key pattern should not be redacted."""
        text = "API_KEY=short"
        result = sanitize_secrets(text)
        # "short" is < 20 chars, should not match
        assert "short" in result
