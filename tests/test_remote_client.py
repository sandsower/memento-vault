"""Tests for the remote vault client."""

import json
import pytest
from unittest.mock import patch, MagicMock

from memento.remote_client import is_remote, list_notes, search, get, store, capture, status


class TestIsRemote:
    def test_false_by_default(self):
        assert not is_remote()

    def test_true_when_url_set(self, monkeypatch):
        monkeypatch.setenv("MEMENTO_VAULT_URL", "http://localhost:8745")
        assert is_remote()


class TestCallTool:
    """Test the HTTP client logic with mocked urllib."""

    def _mock_response(self, result_data):
        """Create a mock JSON-RPC response wrapping MCP tool output."""
        mcp_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(result_data)},
                ]
            },
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mcp_response).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_search(self, mock_urlopen, mock_url):
        results = [
            {"path": "notes/foo.md", "title": "Foo", "score": 0.9, "snippet": "test"},
        ]
        mock_urlopen.return_value = self._mock_response(results)

        found = search("test query")
        assert len(found) == 1
        assert found[0]["title"] == "Foo"

        # Verify the request was made correctly
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data)
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "memento_search"
        assert body["params"]["arguments"]["query"] == "test query"

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_get(self, mock_urlopen, mock_url):
        note = {"path": "notes/foo.md", "title": "Foo", "content": "Body"}
        mock_urlopen.return_value = self._mock_response(note)

        result = get("notes/foo.md")
        assert result is not None
        assert result["title"] == "Foo"

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_store(self, mock_urlopen, mock_url):
        store_result = {"path": "notes/test-note.md", "title": "Test Note"}
        mock_urlopen.return_value = self._mock_response(store_result)

        result = store("Test Note", "Body content", tags=["test"])
        assert result["path"] == "notes/test-note.md"

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_capture(self, mock_urlopen, mock_url):
        capture_result = {"session_id": "abc123", "note_path": "notes/session.md"}
        mock_urlopen.return_value = self._mock_response(capture_result)

        result = capture("Session summary", cwd="/home/user/project")
        assert result["session_id"] == "abc123"

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_status(self, mock_urlopen, mock_url):
        status_result = {"vault_id": "abc", "note_count": 42}
        mock_urlopen.return_value = self._mock_response(status_result)

        result = status()
        assert result["note_count"] == 42

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client._api_key", return_value="test-key")
    @patch("memento.remote_client.request.urlopen")
    def test_auth_header_sent(self, mock_urlopen, mock_key, mock_url):
        mock_urlopen.return_value = self._mock_response([])

        search("test")

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer test-key"

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_search_returns_empty_on_error(self, mock_urlopen, mock_url):
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection refused")

        result = search("test")
        assert result == []

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_url_gets_mcp_suffix(self, mock_urlopen, mock_url):
        mock_urlopen.return_value = self._mock_response([])

        search("test")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "http://localhost:8745/mcp"

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745/mcp")
    @patch("memento.remote_client.request.urlopen")
    def test_url_no_double_mcp_suffix(self, mock_urlopen, mock_url):
        mock_urlopen.return_value = self._mock_response([])

        search("test")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "http://localhost:8745/mcp"

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_list_notes(self, mock_urlopen, mock_url):
        inventory = [
            {"path": "notes/foo.md", "title": "Foo", "hash": "abc123"},
            {"path": "notes/bar.md", "title": "Bar", "hash": "def456"},
        ]
        mock_urlopen.return_value = self._mock_response(inventory)

        result = list_notes()
        assert len(result) == 2
        assert result[0]["path"] == "notes/foo.md"
        assert result[1]["hash"] == "def456"

        body = json.loads(mock_urlopen.call_args[0][0].data)
        assert body["params"]["name"] == "memento_list"

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_list_notes_returns_empty_on_error(self, mock_urlopen, mock_url):
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection refused")

        result = list_notes()
        assert result == []
