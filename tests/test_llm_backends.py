"""Tests for the shared LLM backend abstraction."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from memento.llm import LLMResult, llm_complete, preflight_check


class TestLlmResult:
    def test_llm_result_dataclass(self):
        result = LLMResult(text="ok", ok=True, error=None)

        assert result.text == "ok"
        assert result.ok is True
        assert result.error is None


class TestCliBackends:
    @patch("memento.llm.subprocess.run")
    def test_claude_backend_builds_correct_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="claude output\n", stderr="")

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "claude",
                "llm_model": "sonnet",
            },
        )

        cmd = mock_run.call_args[0][0]
        assert cmd == ["claude", "--print", "--model", "sonnet", "-p", "test prompt"]
        assert result.ok is True
        assert result.text == "claude output"

    @patch("memento.llm.Path.read_text", return_value="codex output\n")
    @patch("memento.llm.Path.unlink")
    @patch("memento.llm.subprocess.run")
    def test_codex_backend_builds_correct_command(self, mock_run, mock_unlink, mock_read):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "codex",
                "llm_model": "gpt-5",
            },
        )

        cmd = mock_run.call_args[0][0]
        assert cmd[0:3] == ["codex", "exec", "--ephemeral"]
        assert "-o" in cmd
        assert "--model" in cmd
        assert "gpt-5" in cmd
        assert cmd[-1] == "test prompt"
        assert result.ok is True
        assert result.text == "codex output"
        mock_unlink.assert_called_once()
        mock_read.assert_called_once()

    @patch("memento.llm.subprocess.run")
    def test_gemini_backend_builds_correct_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="gemini output\n", stderr="")

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "gemini",
                "llm_model": "gemini-2.5-pro",
            },
        )

        cmd = mock_run.call_args[0][0]
        assert cmd == ["gemini", "--model", "gemini-2.5-pro", "-p", "test prompt"]
        assert result.ok is True
        assert result.text == "gemini output"

    @patch("memento.llm.subprocess.run")
    def test_backend_returns_error_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)

        result = llm_complete("prompt", {"llm_backend": "claude"})

        assert result.ok is False
        assert "timed out" in result.error.lower()

    @patch("memento.llm.subprocess.run")
    def test_backend_returns_error_on_missing_binary(self, mock_run):
        mock_run.side_effect = FileNotFoundError("claude not found")

        result = llm_complete("prompt", {"llm_backend": "claude"})

        assert result.ok is False
        assert "not found" in result.error.lower()

    @patch("memento.llm.subprocess.run")
    def test_backend_returns_error_on_empty_response(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="   \n", stderr="")

        result = llm_complete("prompt", {"llm_backend": "claude"})

        assert result.ok is False
        assert "empty" in result.error.lower()

    @patch("memento.llm.subprocess.run")
    def test_backend_checks_return_code(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")

        result = llm_complete("prompt", {"llm_backend": "claude"})

        assert result.ok is False
        assert "boom" in result.error

    @patch("memento.llm.subprocess.run")
    def test_get_backend_from_config(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")

        with patch("memento.llm.get_config", return_value={"llm_backend": "claude", "agent_model": "haiku"}):
            result = llm_complete("prompt")

        cmd = mock_run.call_args[0][0]
        assert cmd == ["claude", "--print", "--model", "haiku", "-p", "prompt"]
        assert result.ok is True

    @patch("memento.llm.subprocess.run")
    def test_preflight_check_claude(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="1.0.0\n", stderr="")

        ok, message = preflight_check({"llm_backend": "claude"})

        assert ok is True
        assert "claude" in message.lower()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["claude", "--version"]


class TestApiBackends:
    @patch("memento.llm.request.urlopen")
    def test_anthropic_api_backend_sends_correct_request(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"content": [{"type": "text", "text": "anthropic output"}]}
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = response

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "anthropic-api",
                "llm_model": "claude-3-5-sonnet-latest",
                "llm_api_key": "secret",
            },
        )

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode())
        assert req.full_url == "https://api.anthropic.com/v1/messages"
        assert req.headers["x-api-key"] == "secret"
        assert payload["model"] == "claude-3-5-sonnet-latest"
        assert payload["messages"][0]["content"] == "test prompt"
        assert result.ok is True
        assert result.text == "anthropic output"

    @patch("memento.llm.request.urlopen")
    def test_openai_compat_backend_sends_correct_request(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": "openai output"}}]}
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = response

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "openai-compat",
                "llm_model": "gpt-5",
                "llm_api_key": "secret",
                "llm_api_base": "https://example.test/v1",
            },
        )

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode())
        assert req.full_url == "https://example.test/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer secret"
        assert payload["model"] == "gpt-5"
        assert payload["messages"][0]["content"] == "test prompt"
        assert result.ok is True
        assert result.text == "openai output"
