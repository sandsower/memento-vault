"""Regression tests for register_mcp_cli in lib/install-lib.sh.

Covers the failure path Codex flagged: when `claude mcp add` / `codex mcp add`
fails after the corresponding `mcp remove` succeeds, the installer must NOT
exit silently. It must surface the failure, show the prior config, and print
the recovery command — without aborting the whole install via `set -e`.
"""

import os
import shutil
import stat
import subprocess
import sys
import textwrap

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO_ROOT, "lib", "install-lib.sh")


def _write_shim(bin_dir, name, body):
    """Create an executable shell shim at bin_dir/name."""
    path = os.path.join(bin_dir, name)
    with open(path, "w") as f:
        f.write("#!/usr/bin/env bash\n")
        f.write(body)
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _run_register(tmp_path, claude_body, codex_body, env_overrides=None):
    """Invoke register_mcp_cli with fake claude/codex shims on PATH.

    Returns CompletedProcess. Uses `set -euo pipefail` to mirror install.sh —
    a failing add must not bring down the script.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if claude_body is not None:
        _write_shim(str(bin_dir), "claude", claude_body)
    if codex_body is not None:
        _write_shim(str(bin_dir), "codex", codex_body)

    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()

    script = textwrap.dedent(f"""
        set -euo pipefail
        # Globals expected by install-lib.sh
        SCRIPT_DIR="{REPO_ROOT}"
        CLAUDE_DIR="{claude_dir}"
        VAULT_PATH="{tmp_path}/vault"
        CONFIG_DIR="{tmp_path}/config"
        MANIFEST="{tmp_path}/config/manifest.json"
        NEW_VERSION="0.0.0"
        FORCE=false
        EXPERIMENTAL=false
        MCP_INSTALL=true
        REMOTE_MODE=${{REMOTE_MODE:-false}}
        REMOTE_URL="${{REMOTE_URL:-}}"
        REMOTE_API_KEY="${{REMOTE_API_KEY:-}}"
        MANIFEST_FILES_JSON="{{}}"
        QMD_AVAILABLE=false

        source "{LIB}"
        register_mcp_cli
        echo "REGISTER_RC=$?"
        echo "SCRIPT_REACHED_END"
    """)

    # Isolate PATH to bin_dir + minimal system dirs so the real `claude`/`codex`
    # CLIs on the developer's machine cannot leak into the test.
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    if env_overrides:
        env.update(env_overrides)

    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_local_mode_both_clis_succeed(self, tmp_path):
        ok = 'echo "ok $@"; exit 0\n'
        result = _run_register(tmp_path, claude_body=ok, codex_body=ok)
        assert result.returncode == 0, result.stderr
        assert "registered with Claude Code" in result.stdout
        assert "registered with Codex" in result.stdout
        assert "SCRIPT_REACHED_END" in result.stdout

    def test_missing_clis_prints_manual_instructions(self, tmp_path):
        # No shims at all — both CLIs absent.
        result = _run_register(tmp_path, claude_body=None, codex_body=None)
        assert result.returncode == 0, result.stderr
        assert "Claude Code CLI not found" in result.stdout
        assert "Codex CLI not found" in result.stdout
        assert "SCRIPT_REACHED_END" in result.stdout


# ---------------------------------------------------------------------------
# Failure paths — the regression Codex flagged
# ---------------------------------------------------------------------------


class TestAddFailureDoesNotKillInstaller:
    def test_claude_add_failure_warns_and_continues(self, tmp_path):
        """If claude mcp add fails after remove succeeds, installer must
        NOT exit via set -e — it must warn, show recovery command, and
        proceed to the Codex registration."""
        # `get` returns a snapshot; `remove` succeeds; `add` fails.
        claude_body = textwrap.dedent("""
            case "$1 ${2:-}" in
                "mcp get") echo "memento-vault: http http://old.example.com/mcp"; exit 0 ;;
                "mcp remove") exit 0 ;;
                "mcp add") echo "boom: unsupported flag" >&2; exit 2 ;;
                *) exit 0 ;;
            esac
        """)
        codex_ok = 'echo "ok $@"; exit 0\n'

        result = _run_register(tmp_path, claude_body=claude_body, codex_body=codex_ok)

        # Crucially: the script must reach the end despite set -e.
        assert "SCRIPT_REACHED_END" in result.stdout, result.stdout + result.stderr
        assert result.returncode == 0, result.stderr
        assert "claude mcp add failed" in result.stdout
        assert "Re-register manually" in result.stdout
        # Prior snapshot surfaced.
        assert "old.example.com" in result.stdout
        # Codex registration still attempted after Claude failure.
        assert "registered with Codex" in result.stdout

    def test_codex_add_failure_warns_and_continues(self, tmp_path):
        claude_ok = 'echo "ok $@"; exit 0\n'
        codex_body = textwrap.dedent("""
            case "$1 ${2:-}" in
                "mcp get") echo "memento-vault: stdio python3 -m memento"; exit 0 ;;
                "mcp remove") exit 0 ;;
                "mcp add") echo "boom: write error" >&2; exit 3 ;;
                *) exit 0 ;;
            esac
        """)

        result = _run_register(tmp_path, claude_body=claude_ok, codex_body=codex_body)

        assert "SCRIPT_REACHED_END" in result.stdout, result.stdout + result.stderr
        assert result.returncode == 0
        assert "codex mcp add failed" in result.stdout
        assert "Re-register manually" in result.stdout
        assert "memento-vault: stdio" in result.stdout

    def test_both_clis_fail_installer_still_completes(self, tmp_path):
        fail = textwrap.dedent("""
            case "$1 ${2:-}" in
                "mcp get") echo "snapshot"; exit 0 ;;
                "mcp remove") exit 0 ;;
                "mcp add") echo "fail" >&2; exit 1 ;;
                *) exit 0 ;;
            esac
        """)

        result = _run_register(tmp_path, claude_body=fail, codex_body=fail)
        assert "SCRIPT_REACHED_END" in result.stdout, result.stdout + result.stderr
        assert result.returncode == 0
        assert result.stdout.count("Re-register manually") == 2


class TestCodexRemotePreflight:
    def test_codex_without_url_flag_skips_destructive_remove(self, tmp_path):
        """Old Codex CLI lacking --url support must NOT have its existing
        registration removed. Preserve it and tell the user to upgrade."""
        # `mcp add --help` does NOT mention --url.
        codex_body = textwrap.dedent("""
            if [ "$1" = "mcp" ] && [ "$2" = "add" ] && [ "${3:-}" = "--help" ]; then
                echo "Usage: codex mcp add NAME -- COMMAND"
                exit 0
            fi
            if [ "$1" = "mcp" ] && [ "$2" = "remove" ]; then
                # If we got here, the test failed — destructive remove ran.
                echo "DESTRUCTIVE_REMOVE_RAN" >&2
                exit 0
            fi
            if [ "$1" = "mcp" ] && [ "$2" = "add" ]; then
                echo "DESTRUCTIVE_ADD_RAN" >&2
                exit 0
            fi
            exit 0
        """)
        claude_ok = 'echo "ok $@"; exit 0\n'

        result = _run_register(
            tmp_path,
            claude_body=claude_ok,
            codex_body=codex_body,
            env_overrides={
                "REMOTE_MODE": "true",
                "REMOTE_URL": "https://vault.example.com",
                "REMOTE_API_KEY": "secret",
            },
        )

        assert result.returncode == 0, result.stderr
        assert "SCRIPT_REACHED_END" in result.stdout
        assert "does not support '--url'" in result.stdout
        assert "DESTRUCTIVE_REMOVE_RAN" not in result.stderr
        assert "DESTRUCTIVE_ADD_RAN" not in result.stderr

    def test_codex_without_bearer_flag_skips_when_key_present(self, tmp_path):
        codex_body = textwrap.dedent("""
            if [ "$1" = "mcp" ] && [ "$2" = "add" ] && [ "${3:-}" = "--help" ]; then
                echo "Usage: codex mcp add NAME --url URL"
                exit 0
            fi
            if [ "$1" = "mcp" ] && [ "$2" = "remove" ]; then
                echo "DESTRUCTIVE_REMOVE_RAN" >&2
                exit 0
            fi
            if [ "$1" = "mcp" ] && [ "$2" = "add" ]; then
                echo "DESTRUCTIVE_ADD_RAN" >&2
                exit 0
            fi
            exit 0
        """)
        claude_ok = 'echo "ok $@"; exit 0\n'

        result = _run_register(
            tmp_path,
            claude_body=claude_ok,
            codex_body=codex_body,
            env_overrides={
                "REMOTE_MODE": "true",
                "REMOTE_URL": "https://vault.example.com",
                "REMOTE_API_KEY": "secret",
            },
        )

        assert "SCRIPT_REACHED_END" in result.stdout
        assert "does not support '--bearer-token-env-var'" in result.stdout
        assert "DESTRUCTIVE_REMOVE_RAN" not in result.stderr
        assert "DESTRUCTIVE_ADD_RAN" not in result.stderr
