"""Integration smoke test: real HTTP round-trip to MCP server.

Spins up the MCP server in a subprocess on a random free port, makes real
remote_client calls, and verifies end-to-end JSON-RPC + auth + stateless HTTP.

Skipped in CI (needs a free port). Run locally with:
    pytest tests/test_integration_remote.py -v
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(port: int, timeout: float = 10.0) -> bool:
    """Poll until the server accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Start the MCP server in a subprocess with a temp vault."""
    port = _free_port()
    vault = tmp_path_factory.mktemp("vault")
    (vault / "notes").mkdir()
    (vault / "fleeting").mkdir()
    (vault / "projects").mkdir()

    # Write a test note so search/get have something to find
    note = vault / "notes" / "test-integration-note.md"
    note.write_text(
        "---\ntitle: Integration Test Note\ntype: discovery\n"
        "session_id: integration-test-session\n---\n\n"
        "This note verifies the remote xylophone round-trip works end to end.\n"
    )

    api_key = "test-integration-key-12345"
    # Strip qmd from PATH so the server uses GrepBackend (searches the temp vault
    # instead of the real QMD collection which doesn't have our test notes)
    clean_path = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if not os.path.isfile(os.path.join(p, "qmd"))
    )
    env = {
        **os.environ,
        "PATH": clean_path,
        "MEMENTO_VAULT_PATH": str(vault),
        "MEMENTO_TRANSPORT": "streamable-http",
        "MEMENTO_HOST": "127.0.0.1",
        "MEMENTO_PORT": str(port),
        "MEMENTO_API_KEY": api_key,
        "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    }

    proc = subprocess.Popen(
        [sys.executable, "-m", "memento", "--transport", "streamable-http"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if not _wait_for_server(port):
        proc.kill()
        out, err = proc.communicate(timeout=5)
        pytest.fail(f"Server failed to start on port {port}.\nstdout: {out.decode()}\nstderr: {err.decode()}")

    yield {"port": port, "api_key": api_key, "vault": vault, "proc": proc}

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(autouse=True)
def _set_remote_env(server, monkeypatch):
    """Point remote_client at the test server."""
    monkeypatch.setenv("MEMENTO_VAULT_URL", f"http://127.0.0.1:{server['port']}")
    monkeypatch.setenv("MEMENTO_API_KEY", server["api_key"])


class TestRemoteIntegration:
    def test_status(self):
        from memento.remote_client import status

        result = status()
        assert "vault_id" in result or "vault_path" in result
        assert "error" not in result

    def test_search(self):
        from memento.remote_client import search

        results = search("xylophone")
        assert len(results) >= 1
        assert any("integration" in r.get("title", "").lower() or "xylophone" in r.get("content", "").lower() for r in results)
        assert any("integration" in r.get("title", "").lower() or "xylophone" in r.get("content", "").lower() for r in results)

    def test_search_returns_content(self):
        from memento.remote_client import search

        results = search("xylophone")
        assert len(results) >= 1
        # Verify content field is present (option A — inline content)
        assert any(r.get("content") for r in results)

    def test_get(self):
        from memento.remote_client import get

        result = get("notes/test-integration-note.md")
        assert result is not None
        assert result["title"] == "Integration Test Note"
        assert "xylophone" in result["content"]

    def test_capture_and_dedup(self):
        from memento.remote_client import capture

        result = capture(
            session_summary="Integration test session summary",
            cwd="/tmp/test",
            session_id="integration-smoke-test",
            agent="pytest",
            fleeting_only=True,
        )
        assert "error" not in result
        assert result.get("session_id") == "integration-smoke-test"

        # Second capture with same session_id should be deduplicated
        result2 = capture(
            session_summary="Duplicate",
            cwd="/tmp/test",
            session_id="integration-smoke-test",
            agent="pytest",
            fleeting_only=True,
        )
        assert result2.get("deduplicated") is True

    def test_auth_rejected_without_key(self, server, monkeypatch):
        from memento.remote_client import status

        monkeypatch.delenv("MEMENTO_API_KEY")
        result = status()
        assert "error" in result
