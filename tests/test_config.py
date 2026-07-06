"""Tests for memento.config module."""

import builtins
import os

import pytest
from pathlib import Path
from unittest.mock import patch

import importlib

from memento.config import (
    DEFAULT_CONFIG,
    detect_project,
    ensure_runtime_dir,
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
        assert DEFAULT_CONFIG["claude_bare_headless"] is False

    def test_recall_diagnostics_defaults_disabled(self):
        assert DEFAULT_CONFIG["recall_diagnostics"] is False
        assert DEFAULT_CONFIG["recall_diagnostics_include_candidates"] is False
        assert DEFAULT_CONFIG["recall_diagnostics_max_candidates"] == 10

    def test_recall_concrete_mode_defaults_disabled(self):
        assert DEFAULT_CONFIG["recall_concrete_mode"] is False

    def test_tool_context_defaults_are_tightly_gated_and_diagnostic(self):
        assert DEFAULT_CONFIG["tool_context_min_score"] == 0.75
        assert DEFAULT_CONFIG["tool_context_diagnostics"] is True
        assert DEFAULT_CONFIG["tool_context_diagnostics_include_candidates"] is False
        assert DEFAULT_CONFIG["tool_context_diagnostics_max_candidates"] == 10

    def test_simple_yaml_coerces_tool_context_min_score_string(self, tmp_path):
        config_dir = tmp_path / ".config" / "memento-vault"
        config_dir.mkdir(parents=True)
        (config_dir / "memento.yml").write_text('tool_context_min_score: "0.82"\n')
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError
            return real_import(name, *args, **kwargs)

        with patch.object(Path, "home", return_value=tmp_path), patch("builtins.__import__", side_effect=fake_import):
            reset_config()
            config = load_config()

        assert config["tool_context_min_score"] == 0.82
        assert isinstance(config["tool_context_min_score"], float)

    def test_broad_project_query_skip_defaults_enabled(self):
        assert DEFAULT_CONFIG["recall_skip_broad_project_queries"] is True

    def test_warns_on_corrupt_config_file(self, tmp_path, capsys):
        """Regression: corrupt YAML must warn to stderr, not silently use defaults."""
        vault = tmp_path / "vault"
        vault.mkdir()
        config_file = vault / "memento.yml"
        config_file.write_text(": : : invalid yaml : :\n")
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError
            return real_import(name, *args, **kwargs)

        with (
            patch.object(Path, "home", return_value=tmp_path),
            patch("builtins.__import__", side_effect=fake_import),
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
        """Strict mode probes candidates and returns the first writable one."""
        xdg_runtime = tmp_path / "xdg-runtime"
        fallback_tmp = tmp_path / "tmp"

        with (
            patch.dict("memento.config.os.environ", {"XDG_RUNTIME_DIR": str(xdg_runtime)}, clear=False),
            patch("memento.config.tempfile.gettempdir", return_value=str(fallback_tmp)),
            patch("memento.config._runtime_dir_is_usable", side_effect=[False, False, True]),
        ):
            runtime_dir = get_runtime_dir(strict=True)

        assert runtime_dir == str(fallback_tmp / f"memento-vault-{os.getuid()}")

    def test_non_strict_returns_xdg_path_without_probing(self, tmp_path):
        """Non-strict default returns the preferred path string without probing or mkdirs."""
        xdg_runtime = tmp_path / "xdg-runtime"

        with (
            patch.dict("memento.config.os.environ", {"XDG_RUNTIME_DIR": str(xdg_runtime)}, clear=False),
            patch("memento.config._runtime_dir_is_usable", side_effect=AssertionError("must not probe")),
        ):
            runtime_dir = get_runtime_dir()

        assert runtime_dir == str(xdg_runtime / "memento-vault")
        # Non-strict must not create the directory.
        assert not (xdg_runtime / "memento-vault").exists()

    def test_non_strict_never_raises_when_no_candidate_is_writable(self, tmp_path):
        """Non-strict returns a best-effort path even when strict probing would fail all candidates."""
        xdg_runtime = tmp_path / "xdg-runtime"
        fallback_tmp = tmp_path / "tmp"

        with (
            patch.dict("memento.config.os.environ", {"XDG_RUNTIME_DIR": str(xdg_runtime)}, clear=False),
            patch("memento.config.tempfile.gettempdir", return_value=str(fallback_tmp)),
            patch("memento.config._runtime_dir_is_usable", return_value=False),
        ):
            # Must not raise — import-time resolution is fault-tolerant.
            runtime_dir = get_runtime_dir()

        assert runtime_dir == str(xdg_runtime / "memento-vault")

    def test_strict_raises_when_all_candidates_unwritable(self, tmp_path):
        """Strict mode raises OSError when no candidate is writable."""
        xdg_runtime = tmp_path / "xdg-runtime"
        fallback_tmp = tmp_path / "tmp"

        with (
            patch.dict("memento.config.os.environ", {"XDG_RUNTIME_DIR": str(xdg_runtime)}, clear=False),
            patch("memento.config.tempfile.gettempdir", return_value=str(fallback_tmp)),
            patch("memento.config._runtime_dir_is_usable", return_value=False),
        ):
            with pytest.raises(OSError):
                get_runtime_dir(strict=True)

    def test_ensure_runtime_dir_creates_and_returns_writable_path(self, tmp_path):
        """ensure_runtime_dir creates the resolved directory and returns it."""
        xdg_runtime = tmp_path / "xdg-runtime"

        with (
            patch.dict("memento.config.os.environ", {"XDG_RUNTIME_DIR": str(xdg_runtime)}, clear=False),
            patch("memento.config._runtime_dir_is_usable", side_effect=lambda p: True),
        ):
            path = ensure_runtime_dir()

        assert path == str(xdg_runtime / "memento-vault")
        assert os.path.isdir(path)

    def test_module_import_is_tolerant_under_read_only_sandbox(self, monkeypatch):
        """Reloading memento.config with no writable candidates must not raise at import time."""
        import memento.config as mod

        # Simulate a read-only sandbox: no XDG, no writable candidate.
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

        with patch("memento.config._runtime_dir_is_usable", return_value=False):
            importlib.reload(mod)

        try:
            assert isinstance(mod.RUNTIME_DIR, str)
            assert mod.RUNTIME_DIR  # non-empty path
        finally:
            # Restore real runtime dir so other tests don't inherit the fake sandbox.
            importlib.reload(mod)


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
