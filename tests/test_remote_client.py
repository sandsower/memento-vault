"""Tests for the remote vault client."""

import json
from unittest.mock import patch, MagicMock

from memento.remote_client import (
    is_remote,
    list_notes,
    query,
    search,
    search_envelope,
    get,
    store,
    smart_store,
    capture_run_lesson,
    synthesize_failures,
    capture,
    status,
)


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
        mock_urlopen.return_value = self._mock_response({"results": results, "metadata": {"detail_level": "summary"}})

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
        assert body["params"]["arguments"]["concrete"] == "auto"
        assert body["params"]["arguments"]["detail_level"] == "summary"
        assert body["params"]["arguments"]["include_content"] is False
        assert body["params"]["arguments"]["token_budget"] == 2000

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_search_forwards_concrete_option(self, mock_urlopen, mock_url):
        mock_urlopen.return_value = self._mock_response({"results": [], "metadata": {"detail_level": "summary"}})

        search("MEMENTO_VAULT_PATH", concrete=True)

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["params"]["arguments"]["concrete"] is True

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_search_forwards_response_controls(self, mock_urlopen, mock_url):
        mock_urlopen.return_value = self._mock_response({"results": [], "metadata": {"detail_level": "full"}})

        search_envelope("MEMENTO_VAULT_PATH", detail_level="full", include_content=True, token_budget=99)

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["params"]["arguments"]["detail_level"] == "full"
        assert body["params"]["arguments"]["include_content"] is True
        assert body["params"]["arguments"]["token_budget"] == 99

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_search_prefers_structured_content_result(self, mock_urlopen, mock_url):
        mcp_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"path": "notes/foo.md", "title": "Foo", "score": 0.9}),
                    }
                ],
                "structuredContent": {
                    "result": [
                        {"path": "notes/foo.md", "title": "Foo", "score": 0.9},
                    ]
                },
            },
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mcp_response).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        found = search("test query")

        assert len(found) == 1
        assert found[0]["title"] == "Foo"

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_search_preserves_single_content_block_result_as_list(self, mock_urlopen, mock_url):
        mock_urlopen.return_value = self._mock_response(
            {"path": "notes/foo.md", "title": "Foo", "score": 0.9, "snippet": "test"}
        )

        found = search("test query")

        assert found == [{"path": "notes/foo.md", "title": "Foo", "score": 0.9, "snippet": "test"}]

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_list_notes_preserves_single_content_block_result_as_list(self, mock_urlopen, mock_url):
        inventory = {"path": "notes/foo.md", "title": "Foo", "hash": "abc123"}
        mock_urlopen.return_value = self._mock_response(inventory)

        result = list_notes()

        assert result == [inventory]

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_search_unwraps_structured_result_envelope(self, mock_urlopen, mock_url):
        mock_urlopen.return_value = self._mock_response(
            {
                "results": [{"path": "notes/foo.md", "title": "Foo", "score": 0.9, "snippet": "test"}],
                "miss": None,
            }
        )

        found = search("test query")

        assert found == [{"path": "notes/foo.md", "title": "Foo", "score": 0.9, "snippet": "test"}]

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_search_envelope_preserves_structured_miss(self, mock_urlopen, mock_url):
        miss = {"results": [], "miss": {"reason": "threshold_too_high", "recovery_hints": ["Lower min_score."]}}
        mock_urlopen.return_value = self._mock_response(miss)

        assert search_envelope("test query") == miss

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_query(self, mock_urlopen, mock_url):
        payload = {"aggregations": [{"value": "discovery", "count": 2}], "metadata": {"valid": True}}
        mock_urlopen.return_value = self._mock_response(payload)

        result = query(project="/repo/api", tag="cache", aggregate_by="type", certainty_min=3)

        assert result == payload
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["params"]["name"] == "memento_query"
        assert body["params"]["arguments"]["project"] == "/repo/api"
        assert body["params"]["arguments"]["tag"] == "cache"
        assert body["params"]["arguments"]["aggregate_by"] == "type"
        assert body["params"]["arguments"]["certainty_min"] == 3

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
    def test_smart_store(self, mock_urlopen, mock_url):
        smart_result = {"decision": "candidate_update", "created": False, "path": "notes/test-note.md"}
        mock_urlopen.return_value = self._mock_response(smart_result)

        result = smart_store("Test Note", "Body content", tags=["test"])
        assert result["decision"] == "candidate_update"

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["params"]["name"] == "memento_store_smart"

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_capture_run_lesson(self, mock_urlopen, mock_url):
        capture_result = {"queued": True, "id": "arl-1"}
        mock_urlopen.return_value = self._mock_response(capture_result)

        candidate = {"external_system": "rondo", "run_id": "run-1", "title": "Lesson", "evidence_summary": "Summary"}
        result = capture_run_lesson(candidate, approve_write=True)
        assert result["queued"] is True

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["params"]["name"] == "memento_capture_run_lesson"
        assert body["params"]["arguments"]["candidate"] == candidate
        assert body["params"]["arguments"]["approve_write"] is True

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_synthesize_failures(self, mock_urlopen, mock_url):
        synthesis_result = {"dry_run": True, "candidate_lessons": []}
        mock_urlopen.return_value = self._mock_response(synthesis_result)

        result = synthesize_failures([{"run_id": "r1", "summary": "pytest failed"}], project="/repo")
        assert result["dry_run"] is True

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["params"]["name"] == "memento_synthesize_failures"
        assert body["params"]["arguments"]["project"] == "/repo"
        assert body["params"]["arguments"]["approve_writes"] is False

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
    def test_list_notes_returns_none_on_error(self, mock_urlopen, mock_url):
        """list_notes() returns None on error to distinguish from empty remote.

        Returning [] would conflate a genuinely-empty vault with a failed call,
        which makes --catch-up dangerously bulk-push on network errors.
        """
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection refused")

        result = list_notes()
        assert result is None

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_list_notes_returns_empty_list_when_remote_empty(self, mock_urlopen, mock_url):
        """An actual empty remote returns [], not None."""
        mock_urlopen.return_value = self._mock_response([])

        result = list_notes()
        assert result == []

    @patch("memento.remote_client._vault_url", return_value="http://localhost:8745")
    @patch("memento.remote_client.request.urlopen")
    def test_list_notes_returns_none_on_server_error(self, mock_urlopen, mock_url):
        """Server-side errors (JSON-RPC error result) also yield None."""
        error_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found: memento_list"},
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(error_response).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = list_notes()
        assert result is None
