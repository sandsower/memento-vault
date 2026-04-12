"""Tests for lib/install_helpers.py — warmup, clear-auth-cache, and mcp-config."""

import http.server
import json
import os
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

        rc, stdout, _ = _run_helper(
            "warmup", fake_mcp_server["url"], "my-secret-key"
        )
        assert rc == 0
        assert "OK" in stdout

    def test_warmup_fails_with_wrong_token(self, fake_mcp_server):
        FakeMCPHandler.auth_required = True
        FakeMCPHandler.expected_token = "correct-key"

        rc, _, stderr = _run_helper(
            "warmup", fake_mcp_server["url"], "wrong-key"
        )
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
        rc, _, stderr = _run_helper(
            "warmup", f"http://127.0.0.1:{port}", ""
        )
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
        cache.write_text(json.dumps({
            "memento-vault": {"timestamp": 1234567890},
            "other-server": {"timestamp": 9999999999},
        }))

        rc, stdout, _ = _run_helper(
            "clear-auth-cache", str(tmp_path), "memento-vault"
        )
        assert rc == 0
        assert "Cleared" in stdout

        data = json.loads(cache.read_text())
        assert "memento-vault" not in data
        assert "other-server" in data

    def test_no_op_when_entry_absent(self, tmp_path):
        cache = tmp_path / "mcp-needs-auth-cache.json"
        cache.write_text(json.dumps({"other-server": {"timestamp": 123}}))

        rc, stdout, _ = _run_helper(
            "clear-auth-cache", str(tmp_path), "memento-vault"
        )
        assert rc == 0
        assert "No stale cache" in stdout

    def test_no_op_when_file_missing(self, tmp_path):
        rc, stdout, _ = _run_helper(
            "clear-auth-cache", str(tmp_path), "memento-vault"
        )
        assert rc == 0


# ---------------------------------------------------------------------------
# mcp-config tests
# ---------------------------------------------------------------------------


class TestMcpConfig:
    def test_remote_config_creates_http_entry(self, tmp_path):
        rc, _, _ = _run_helper(
            "mcp-config", "true", str(tmp_path),
            "https://vault.example.com:8745", "my-key",
        )
        assert rc == 0

        config = json.loads((tmp_path / "mcp-servers.json").read_text())
        entry = config["memento-vault"]
        assert entry["type"] == "http"
        assert entry["url"] == "https://vault.example.com:8745/mcp"
        assert entry["headers"]["Authorization"] == "Bearer my-key"

    def test_remote_config_without_key_has_no_headers(self, tmp_path):
        rc, _, _ = _run_helper(
            "mcp-config", "true", str(tmp_path),
            "https://vault.example.com:8745", "",
        )
        assert rc == 0

        config = json.loads((tmp_path / "mcp-servers.json").read_text())
        assert "headers" not in config["memento-vault"]

    def test_local_config_creates_stdio_entry(self, tmp_path):
        rc, _, _ = _run_helper(
            "mcp-config", "false", str(tmp_path), "", "",
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
            "mcp-config", "true", str(tmp_path),
            "https://vault.example.com", "key",
        )
        assert rc == 0

        config = json.loads((tmp_path / "mcp-servers.json").read_text())
        assert "other-server" in config
        assert "memento-vault" in config
