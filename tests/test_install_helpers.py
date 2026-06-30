"""Tests for lib/install_helpers.py — warmup, clear-auth-cache, and mcp-config."""

import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

import pytest

HELPERS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "lib",
    "install_helpers.py",
)


def _run_helper(*args):
    """Run install_helpers.py as a subprocess, return (returncode, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, HELPERS, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_install_help_documents_safe_reinstall_and_dangerous_force():
    installer = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "install.sh",
    )

    result = subprocess.run([installer, "--help"], check=True, text=True, capture_output=True)

    assert "--reinstall" in result.stdout
    assert "Safely rerun same-version install" in result.stdout
    assert "DANGEROUS" in result.stdout
    assert "discarding local edits" in result.stdout


def test_cli_help_documents_safe_reinstall_and_dangerous_force():
    cli = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin",
        "memento-vault",
    )

    result = subprocess.run([cli, "help"], check=True, text=True, capture_output=True)

    assert "--reinstall" in result.stdout
    assert "Safely rerun same-version install" in result.stdout
    assert "DANGEROUS" in result.stdout
    assert "health" in result.stdout
    assert "doctor" in result.stdout


def test_cli_health_delegates_to_python_module(tmp_path):
    cli = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin",
        "memento-vault",
    )
    vault = tmp_path / "vault"
    for dirname in ("notes", "fleeting", "projects", "archive"):
        (vault / dirname).mkdir(parents=True, exist_ok=True)
    (vault / ".git").mkdir()
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_RUNTIME_DIR"] = str(tmp_path / "runtime")
    env["MEMENTO_VAULT_PATH"] = str(vault)
    env["MEMENTO_SEARCH_BACKEND"] = "grep"

    result = subprocess.run([cli, "health", "--json"], text=True, capture_output=True, env=env, timeout=10)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] in {"pass", "warn"}
    assert any(check["name"] == "vault" for check in payload["checks"])


def test_noninteractive_force_requires_explicit_env():
    installer = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "install.sh",
    )
    env = os.environ.copy()
    env.pop("MEMENTO_FORCE", None)

    result = subprocess.run([installer, "--force"], input="", text=True, capture_output=True, env=env, timeout=5)

    assert result.returncode == 1
    assert "Refusing non-interactive --force without MEMENTO_FORCE=1" in result.stdout


def test_setup_cli_installs_user_local_symlink(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = f"""
set -euo pipefail
export HOME={tmp_path}
export PATH=/usr/bin:/bin
SCRIPT_DIR={repo}
CLAUDE_DIR={tmp_path}/.claude
VAULT_PATH={tmp_path}/memento
CONFIG_DIR={tmp_path}/.config/memento-vault
MANIFEST=$CONFIG_DIR/manifest.json
NEW_VERSION=0.0.0
FORCE=false
EXPERIMENTAL=false
MCP_INSTALL=false
REMOTE_MODE=false
REMOTE_URL=
REMOTE_API_KEY=
MANIFEST_FILES_JSON='{{}}'
QMD_AVAILABLE=false
source {repo}/lib/install-lib.sh >/dev/null
setup_cli >/dev/null
"""

    subprocess.run(["bash", "-c", script], check=True, text=True, capture_output=True)

    link = tmp_path / ".local" / "bin" / "memento-vault"
    assert link.is_symlink()
    assert os.readlink(link) == os.path.join(repo, "bin", "memento-vault")

    result = subprocess.run([str(link), "version"], check=True, text=True, capture_output=True)
    with open(os.path.join(repo, "VERSION")) as f:
        assert result.stdout.strip() == f.read().strip()


def test_repair_stale_headless_mcp_config_patches_known_bad_installed_copy(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target = tmp_path / "llm.py"
    target.write_text(
        "# local edit preserved\n"
        "cmd = [\n"
        '        "claude",\n'
        '        "--strict-mcp-config",\n'
        '        "--mcp-config",\n'
        '        "{}",\n'
        "]\n"
    )
    script = f"""
set -euo pipefail
export HOME={tmp_path}
SCRIPT_DIR={repo}
CLAUDE_DIR={tmp_path}/.claude
VAULT_PATH={tmp_path}/memento
CONFIG_DIR={tmp_path}/.config/memento-vault
MANIFEST=$CONFIG_DIR/manifest.json
NEW_VERSION=0.0.0
FORCE=false
EXPERIMENTAL=false
MCP_INSTALL=false
REMOTE_MODE=false
REMOTE_URL=
REMOTE_API_KEY=
MANIFEST_FILES_JSON='{{}}'
QMD_AVAILABLE=false
source {repo}/lib/install-lib.sh >/dev/null
repair_stale_headless_mcp_config {target} memento/llm.py
"""

    result = subprocess.run(["bash", "-c", script], check=True, text=True, capture_output=True)

    repaired = target.read_text()
    assert "# local edit preserved" in repaired
    assert '"{}"' not in repaired
    assert "'{\"mcpServers\": {}}'" in repaired
    assert "Repaired stale headless Claude MCP config" in result.stdout


def test_setup_cli_skips_when_cli_is_already_on_path(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    existing_dir = tmp_path / "existing"
    existing_dir.mkdir()
    existing = existing_dir / "memento-vault"
    existing.write_text("#!/usr/bin/env bash\nexit 0\n")
    existing.chmod(0o755)
    script = f"""
set -euo pipefail
export HOME={tmp_path}
export PATH={existing_dir}:/usr/bin:/bin
SCRIPT_DIR={repo}
CLAUDE_DIR={tmp_path}/.claude
VAULT_PATH={tmp_path}/memento
CONFIG_DIR={tmp_path}/.config/memento-vault
MANIFEST=$CONFIG_DIR/manifest.json
NEW_VERSION=0.0.0
FORCE=false
EXPERIMENTAL=false
MCP_INSTALL=false
REMOTE_MODE=false
REMOTE_URL=
REMOTE_API_KEY=
MANIFEST_FILES_JSON='{{}}'
QMD_AVAILABLE=false
source {repo}/lib/install-lib.sh >/dev/null
setup_cli >/dev/null
"""

    subprocess.run(["bash", "-c", script], check=True, text=True, capture_output=True)

    assert not (tmp_path / ".local" / "bin" / "memento-vault").exists()


def test_setup_cli_does_not_overwrite_regular_file(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    existing = bin_dir / "memento-vault"
    existing.write_text("custom")
    script = f"""
set -euo pipefail
export HOME={tmp_path}
SCRIPT_DIR={repo}
CLAUDE_DIR={tmp_path}/.claude
VAULT_PATH={tmp_path}/memento
CONFIG_DIR={tmp_path}/.config/memento-vault
MANIFEST=$CONFIG_DIR/manifest.json
NEW_VERSION=0.0.0
FORCE=false
EXPERIMENTAL=false
MCP_INSTALL=false
REMOTE_MODE=false
REMOTE_URL=
REMOTE_API_KEY=
MANIFEST_FILES_JSON='{{}}'
QMD_AVAILABLE=false
source {repo}/lib/install-lib.sh >/dev/null
setup_cli >/dev/null
"""

    subprocess.run(["bash", "-c", script], check=True, text=True, capture_output=True)

    assert not existing.is_symlink()
    assert existing.read_text() == "custom"


def test_shell_warmup_snippet_uses_memento_cli_without_shell_job_control():
    """The login-shell warmup must not own a background qmd job."""
    install_lib = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "lib",
        "install-lib.sh",
    )
    with open(install_lib) as f:
        contents = f.read()

    assert 'qmd vsearch "warmup" -c memento -n 1 &>/dev/null &' not in re.search(
        r"cat >> \"\$shell_rc\" << WARMUP_EOF\n(?P<body>.*?)\nWARMUP_EOF", contents, re.S
    ).group("body")
    assert 'local warmup_cli="$SCRIPT_DIR/bin/memento-vault"' in contents
    assert "$warmup_cli_quoted warmup >/dev/null 2>&1" in contents


def test_shell_warmup_upgrades_legacy_snippet_in_local_reinstall(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shell_rc = tmp_path / ".zshrc"
    shell_rc.write_text(
        "# Warm QMD embedding model on shell startup (background, silent)\n"
        'command -v qmd &>/dev/null && qmd vsearch "warmup" -c memento -n 1 &>/dev/null &\n'
    )
    script = f"""
set -euo pipefail
export HOME={tmp_path}
export SHELL=/bin/zsh
SCRIPT_DIR={repo}
CLAUDE_DIR={tmp_path}/.claude
VAULT_PATH={tmp_path}/memento
CONFIG_DIR={tmp_path}/.config/memento-vault
MANIFEST=$CONFIG_DIR/manifest.json
NEW_VERSION=0.0.0
FORCE=false
REINSTALL=true
EXPERIMENTAL=true
MCP_INSTALL=false
REMOTE_MODE=false
REMOTE_URL=
REMOTE_API_KEY=
MANIFEST_FILES_JSON='{{}}'
QMD_AVAILABLE=true
source {repo}/lib/install-lib.sh >/dev/null
setup_shell_warmup >/dev/null
"""

    subprocess.run(["bash", "-c", script], check=True, text=True, capture_output=True)

    contents = shell_rc.read_text()
    assert 'qmd vsearch "warmup"' not in contents
    assert "memento-vault warmup" in contents
    assert contents.count("Warm QMD embedding model") == 1


def test_shell_warmup_upgrades_legacy_snippet_even_in_remote_mode(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shell_rc = tmp_path / ".zshrc"
    shell_rc.write_text(
        "# Warm QMD embedding model on shell startup (background, silent)\n"
        'command -v qmd &>/dev/null && qmd vsearch "warmup" -c memento -n 1 &>/dev/null &\n'
    )
    script = f"""
set -euo pipefail
export HOME={tmp_path}
export SHELL=/bin/zsh
SCRIPT_DIR={repo}
CLAUDE_DIR={tmp_path}/.claude
VAULT_PATH={tmp_path}/memento
CONFIG_DIR={tmp_path}/.config/memento-vault
MANIFEST=$CONFIG_DIR/manifest.json
NEW_VERSION=0.0.0
FORCE=false
REINSTALL=false
EXPERIMENTAL=true
MCP_INSTALL=true
REMOTE_MODE=true
REMOTE_URL=https://example.test
REMOTE_API_KEY=
MANIFEST_FILES_JSON='{{}}'
QMD_AVAILABLE=true
source {repo}/lib/install-lib.sh >/dev/null
setup_shell_warmup >/dev/null
"""

    subprocess.run(["bash", "-c", script], check=True, text=True, capture_output=True)

    contents = shell_rc.read_text()
    assert 'qmd vsearch "warmup"' not in contents
    assert "memento-vault warmup" in contents
    assert contents.count("Warm QMD embedding model") == 1


def test_shell_warmup_upgrades_inline_python_snippet(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shell_rc = tmp_path / ".bashrc"
    shell_rc.write_text(
        "# Warm QMD embedding model on shell startup (detached, silent)\n"
        'command -v python3 >/dev/null 2>&1 && python3 -c \'import shutil, subprocess; q = shutil.which("qmd"); q and subprocess.Popen([q, "vsearch", "warmup", "-c", "memento", "-n", "1"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\' >/dev/null 2>&1\n'
    )
    script = f"""
set -euo pipefail
export HOME={tmp_path}
export SHELL=/bin/bash
SCRIPT_DIR={repo}
CLAUDE_DIR={tmp_path}/.claude
VAULT_PATH={tmp_path}/memento
CONFIG_DIR={tmp_path}/.config/memento-vault
MANIFEST=$CONFIG_DIR/manifest.json
NEW_VERSION=0.0.0
FORCE=false
EXPERIMENTAL=true
MCP_INSTALL=false
REMOTE_MODE=false
REMOTE_URL=
REMOTE_API_KEY=
MANIFEST_FILES_JSON='{{}}'
QMD_AVAILABLE=true
source {repo}/lib/install-lib.sh >/dev/null
setup_shell_warmup >/dev/null
"""

    subprocess.run(["bash", "-c", script], check=True, text=True, capture_output=True)

    contents = shell_rc.read_text()
    assert "python3 -c" not in contents
    assert "memento-vault warmup" in contents
    assert contents.count("Warm QMD embedding model") == 1


def test_memento_vault_warmup_returns_success_without_qmd(tmp_path):
    cli = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin",
        "memento-vault",
    )
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin"

    result = subprocess.run([cli, "warmup"], capture_output=True, text=True, env=env, timeout=5)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_memento_vault_warmup_starts_qmd_detached(tmp_path):
    cli = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin",
        "memento-vault",
    )
    log = tmp_path / "qmd.log"
    qmd = tmp_path / "qmd"
    qmd.write_text(f'#!/usr/bin/env bash\necho "$@" >> {log}\n')
    qmd.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env.get('PATH', '')}"

    result = subprocess.run([cli, "warmup"], capture_output=True, text=True, env=env, timeout=5)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    for _ in range(20):
        if log.exists():
            break
        time.sleep(0.05)
    assert log.read_text().strip() == "vsearch warmup -c memento -n 1"


# ---------------------------------------------------------------------------
# Fake MCP server for warmup tests
# ---------------------------------------------------------------------------


class FakeMCPHandler(http.server.BaseHTTPRequestHandler):
    """Minimal JSON-RPC handler that mimics memento-vault /mcp endpoint."""

    # Class-level controls for test scenarios
    fail_count = 0  # Number of requests to reject before succeeding
    auth_required = False
    expected_token = ""
    request_log = []

    def log_message(self, *args):
        pass  # Suppress stderr logging

    def do_POST(self):
        FakeMCPHandler.request_log.append(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}

        # Auth check
        if FakeMCPHandler.auth_required:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {FakeMCPHandler.expected_token}":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Unauthorized"}).encode())
                return

        # Simulate transient failures
        if FakeMCPHandler.fail_count > 0:
            FakeMCPHandler.fail_count -= 1
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"Service Unavailable")
            return

        # Happy path: return initialize response
        response = {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "memento-vault", "version": "1.99.0"},
            },
        }
        payload = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def fake_mcp_server():
    """Start a fake MCP server on a random port, yield its URL, then shut down."""
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), FakeMCPHandler)

    # Reset handler state
    FakeMCPHandler.fail_count = 0
    FakeMCPHandler.auth_required = False
    FakeMCPHandler.expected_token = ""
    FakeMCPHandler.request_log = []

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield {
        "url": f"http://127.0.0.1:{port}",
        "port": port,
        "handler": FakeMCPHandler,
    }

    server.shutdown()


# ---------------------------------------------------------------------------
# warmup tests
# ---------------------------------------------------------------------------


class TestWarmup:
    def test_warmup_succeeds_on_healthy_server(self, fake_mcp_server):
        rc, stdout, stderr = _run_helper("warmup", fake_mcp_server["url"], "")
        assert rc == 0
        assert "OK memento-vault v1.99.0" in stdout

    def test_warmup_appends_mcp_path(self, fake_mcp_server):
        """URL without /mcp should get it appended automatically."""
        rc, stdout, _ = _run_helper("warmup", fake_mcp_server["url"], "")
        assert rc == 0
        assert "/mcp" in FakeMCPHandler.request_log[0]

    def test_warmup_passes_bearer_token(self, fake_mcp_server):
        FakeMCPHandler.auth_required = True
        FakeMCPHandler.expected_token = "my-secret-key"

        rc, stdout, _ = _run_helper("warmup", fake_mcp_server["url"], "my-secret-key")
        assert rc == 0
        assert "OK" in stdout

    def test_warmup_fails_with_wrong_token(self, fake_mcp_server):
        FakeMCPHandler.auth_required = True
        FakeMCPHandler.expected_token = "correct-key"

        rc, _, stderr = _run_helper("warmup", fake_mcp_server["url"], "wrong-key")
        assert rc == 1
        assert "FAIL" in stderr

    def test_warmup_retries_on_transient_failure(self, fake_mcp_server):
        """Server fails twice then succeeds — warmup should retry and pass."""
        FakeMCPHandler.fail_count = 2

        rc, stdout, _ = _run_helper("warmup", fake_mcp_server["url"], "")
        assert rc == 0
        assert "OK" in stdout
        # Should have made 3 requests total (2 failures + 1 success)
        assert len(FakeMCPHandler.request_log) == 3

    def test_warmup_gives_up_after_max_retries(self):
        """Unreachable server should fail after retries."""
        # Use a port that nothing listens on
        port = _free_port()
        rc, _, stderr = _run_helper("warmup", f"http://127.0.0.1:{port}", "")
        assert rc == 1
        assert "FAIL" in stderr

    def test_warmup_no_api_key_omits_auth_header(self, fake_mcp_server):
        """When api_key is empty, no Authorization header should be sent."""
        FakeMCPHandler.auth_required = False
        rc, stdout, _ = _run_helper("warmup", fake_mcp_server["url"], "")
        assert rc == 0
        assert "OK" in stdout


# ---------------------------------------------------------------------------
# clear-auth-cache tests
# ---------------------------------------------------------------------------


class TestClearAuthCache:
    def test_clears_existing_entry(self, tmp_path):
        cache = tmp_path / "mcp-needs-auth-cache.json"
        cache.write_text(
            json.dumps(
                {
                    "memento-vault": {"timestamp": 1234567890},
                    "other-server": {"timestamp": 9999999999},
                }
            )
        )

        rc, stdout, _ = _run_helper("clear-auth-cache", str(tmp_path), "memento-vault")
        assert rc == 0
        assert "Cleared" in stdout

        data = json.loads(cache.read_text())
        assert "memento-vault" not in data
        assert "other-server" in data

    def test_no_op_when_entry_absent(self, tmp_path):
        cache = tmp_path / "mcp-needs-auth-cache.json"
        cache.write_text(json.dumps({"other-server": {"timestamp": 123}}))

        rc, stdout, _ = _run_helper("clear-auth-cache", str(tmp_path), "memento-vault")
        assert rc == 0
        assert "No stale cache" in stdout

    def test_no_op_when_file_missing(self, tmp_path):
        rc, stdout, _ = _run_helper("clear-auth-cache", str(tmp_path), "memento-vault")
        assert rc == 0


# ---------------------------------------------------------------------------
# mcp-config tests
# ---------------------------------------------------------------------------


class TestMcpConfig:
    def test_remote_config_creates_http_entry(self, tmp_path):
        rc, _, _ = _run_helper(
            "mcp-config",
            "true",
            str(tmp_path),
            "https://vault.example.com:8745",
            "my-key",
        )
        assert rc == 0

        config = json.loads((tmp_path / "mcp-servers.json").read_text())
        entry = config["memento-vault"]
        assert entry["type"] == "http"
        assert entry["url"] == "https://vault.example.com:8745/mcp"
        assert entry["headers"]["Authorization"] == "Bearer my-key"

    def test_remote_config_without_key_has_no_headers(self, tmp_path):
        rc, _, _ = _run_helper(
            "mcp-config",
            "true",
            str(tmp_path),
            "https://vault.example.com:8745",
            "",
        )
        assert rc == 0

        config = json.loads((tmp_path / "mcp-servers.json").read_text())
        assert "headers" not in config["memento-vault"]

    def test_local_config_creates_stdio_entry(self, tmp_path):
        rc, _, _ = _run_helper(
            "mcp-config",
            "false",
            str(tmp_path),
            "",
            "",
        )
        assert rc == 0

        config = json.loads((tmp_path / "mcp-servers.json").read_text())
        entry = config["memento-vault"]
        assert entry["command"] == "python3"
        assert "-m" in entry["args"]
        assert "memento" in entry["args"]

    def test_config_merges_with_existing(self, tmp_path):
        existing = {"other-server": {"command": "node", "args": ["server.js"]}}
        (tmp_path / "mcp-servers.json").write_text(json.dumps(existing))

        rc, _, _ = _run_helper(
            "mcp-config",
            "true",
            str(tmp_path),
            "https://vault.example.com",
            "key",
        )
        assert rc == 0

        config = json.loads((tmp_path / "mcp-servers.json").read_text())
        assert "other-server" in config
        assert "memento-vault" in config


class TestMergeSettings:
    def test_default_install_adds_retrieval_hooks_and_permissions(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.json"
        settings_path.write_text(json.dumps({"hooks": {"SessionEnd": []}}))

        result = subprocess.run(
            [
                sys.executable,
                HELPERS,
                "merge-settings",
                str(settings_path),
                str(claude_dir),
                str(tmp_path / "vault"),
                "false",
                "",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0
        merged = json.loads(settings_path.read_text())

        hooks = merged["hooks"]
        assert "SessionEnd" in hooks
        assert "SessionStart" in hooks
        assert "UserPromptSubmit" in hooks
        assert "PreToolUse" in hooks
        assert hooks["SessionEnd"][0]["hooks"][0]["command"].endswith("memento-triage.py")
        assert hooks["SessionStart"][0]["hooks"][0]["command"].endswith("vault-briefing.py")
        assert hooks["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("vault-recall.py")
        assert hooks["PreToolUse"][0]["hooks"][0]["command"].endswith("vault-tool-context.py")

        perms = merged["permissions"]["allow"]
        assert any(rule == f"Read({tmp_path / 'vault'}/**)" for rule in perms)
        assert any(rule == f"Edit({tmp_path / 'vault'}/**)" for rule in perms)
        assert any(rule == f"Write({tmp_path / 'vault'}/**)" for rule in perms)
        assert any(rule.endswith("/hooks/vault-commit.sh:*)") for rule in perms)
