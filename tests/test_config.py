"""Tests for memento.config module."""

import os
from pathlib import Path
from unittest.mock import patch

from memento.config import (
    DEFAULT_CONFIG,
    detect_project,
    get_config,
    get_runtime_dir,
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

    def test_warns_on_corrupt_config_file(self, tmp_path, capsys):
        """Regression: corrupt YAML must warn to stderr, not silently use defaults."""
        vault = tmp_path / "vault"
        vault.mkdir()
        config_file = vault / "memento.yml"
        config_file.write_text(": : : invalid yaml : :\n")

        with (
            patch.object(Path, "home", return_value=tmp_path),
            patch("memento.config.DEFAULT_CONFIG", {**DEFAULT_CONFIG, "vault_path": str(vault)}),
        ):
            reset_config()
            config = load_config()

        captured = capsys.readouterr()
        assert "[memento] warning: failed to parse config" in captured.err
        # Should still return defaults
        assert config["llm_backend"] == "claude"


class TestRuntimeDir:
    def test_falls_back_to_temp_when_primary_locations_are_not_writable(self, tmp_path):
        xdg_runtime = tmp_path / "xdg-runtime"
        fallback_tmp = tmp_path / "tmp"

        with (
            patch.dict("memento.config.os.environ", {"XDG_RUNTIME_DIR": str(xdg_runtime)}, clear=False),
            patch("memento.config.tempfile.gettempdir", return_value=str(fallback_tmp)),
            patch("memento.config._runtime_dir_is_usable", side_effect=[False, False, True]),
        ):
            runtime_dir = get_runtime_dir()

        assert runtime_dir == str(fallback_tmp / f"memento-vault-{os.getuid()}")


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
