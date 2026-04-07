"""Tests for memento.config module."""

from pathlib import Path
from unittest.mock import patch

from memento.config import (
    DEFAULT_CONFIG,
    detect_project,
    get_config,
    get_vault,
    load_config,
    reset_config,
    slugify,
)


class TestGetConfig:
    def setup_method(self):
        reset_config()

    def teardown_method(self):
        reset_config()

    def test_returns_dict_with_defaults(self):
        with patch("memento.config.load_config", return_value=dict(DEFAULT_CONFIG)):
            config = get_config()
        assert isinstance(config, dict)
        assert "vault_path" in config
        assert "agent_model" in config

    def test_caches_result(self):
        with patch("memento.config.load_config", return_value=dict(DEFAULT_CONFIG)) as mock:
            get_config()
            get_config()
        assert mock.call_count == 1

    def test_reset_clears_cache(self):
        with patch("memento.config.load_config", return_value=dict(DEFAULT_CONFIG)) as mock:
            get_config()
            reset_config()
            get_config()
        assert mock.call_count == 2


class TestGetVault:
    def setup_method(self):
        reset_config()

    def teardown_method(self):
        reset_config()

    def test_returns_path(self):
        with patch("memento.config.load_config", return_value=dict(DEFAULT_CONFIG)):
            vault = get_vault()
        assert isinstance(vault, Path)

    def test_matches_config(self):
        with patch("memento.config.load_config", return_value=dict(DEFAULT_CONFIG)):
            vault = get_vault()
            config = get_config()
        assert str(vault) == config["vault_path"]


class TestLoadConfig:
    def test_returns_defaults_when_no_file(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            reset_config()
            config = load_config()
        assert config["agent_model"] == "sonnet"
        assert config["recall_max_notes"] == 3

    def test_includes_llm_backend_defaults(self):
        assert "llm_backend" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["llm_backend"] == "claude"
        assert DEFAULT_CONFIG["llm_model"] is None


class TestSlugify:
    def test_basic(self):
        assert slugify("Hello World") == "hello-world"

    def test_special_chars(self):
        assert slugify("foo/bar@baz") == "foobarbaz"

    def test_empty(self):
        assert slugify("") == ""

    def test_truncates_at_80(self):
        long = "a" * 100
        assert len(slugify(long)) == 80


class TestDetectProject:
    def setup_method(self):
        reset_config()

    def teardown_method(self):
        reset_config()

    def test_returns_slug_from_cwd(self):
        with patch("memento.config.get_config", return_value=dict(DEFAULT_CONFIG)):
            slug, ticket = detect_project("/home/user/Projects/my-project", None)
        assert slug == "my-project"
        assert ticket is None

    def test_extracts_ticket_from_branch(self):
        with patch("memento.config.get_config", return_value=dict(DEFAULT_CONFIG)):
            slug, ticket = detect_project("/home/user/Projects/app", "feature/DAL-123-add-login")
        assert ticket == "DAL-123"

    def test_unknown_when_no_cwd(self):
        slug, ticket = detect_project(None, None)
        assert slug == "unknown"
