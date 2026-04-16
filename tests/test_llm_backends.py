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
        assert mock_run.call_args.kwargs["stdin"] == subprocess.DEVNULL

    @patch("memento.llm.Path.read_text", return_value='{"notes":[]}\n')
    @patch("memento.llm.Path.unlink")
    @patch("memento.llm.subprocess.run")
    def test_codex_backend_uses_output_file_when_cli_exits_nonzero(self, mock_run, mock_unlink, mock_read):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "codex",
                "llm_model": "gpt-5",
            },
        )

        assert result.ok is True
        assert result.text == '{"notes":[]}'
        mock_unlink.assert_called_once()
        mock_read.assert_called_once()

    @patch("memento.llm.Path.read_text", return_value="")
    @patch("memento.llm.Path.unlink")
    @patch("memento.llm.subprocess.run")
    def test_codex_backend_falls_back_to_stdout_when_output_file_is_empty(self, mock_run, mock_unlink, mock_read):
        mock_run.return_value = MagicMock(returncode=0, stdout='{"notes":[]}\n', stderr="")

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "codex",
                "llm_model": "gpt-5",
            },
        )

        assert result.ok is True
        assert result.text == '{"notes":[]}'
        mock_unlink.assert_called_once()
        mock_read.assert_called_once()

    @patch("memento.llm.Path.read_text", return_value="")
    @patch("memento.llm.Path.unlink")
    @patch("memento.llm.subprocess.run")
    def test_codex_backend_retries_once_after_transient_cli_failure(self, mock_run, mock_unlink, mock_read):
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=0, stdout='{"notes":[]}\n', stderr=""),
        ]

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "codex",
                "llm_model": "gpt-5",
            },
        )

        assert result.ok is True
        assert result.text == '{"notes":[]}'
        assert mock_run.call_count == 2

    @patch("memento.llm.Path.read_text", return_value="")
    @patch("memento.llm.Path.unlink")
    @patch("memento.llm.subprocess.run")
    def test_codex_backend_retries_until_third_attempt_succeeds(self, mock_run, mock_unlink, mock_read):
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=0, stdout='{"notes":[]}\n', stderr=""),
        ]

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "codex",
                "llm_model": "gpt-5",
            },
        )

        assert result.ok is True
        assert result.text == '{"notes":[]}'
        assert mock_run.call_count == 3

    @patch("memento.llm.Path.read_text", return_value="")
    @patch("memento.llm.Path.unlink")
    @patch("memento.llm.subprocess.run")
    def test_codex_backend_retries_until_fifth_attempt_succeeds(self, mock_run, mock_unlink, mock_read):
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=0, stdout='{"notes":[]}\n', stderr=""),
        ]

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "codex",
                "llm_model": "gpt-5",
            },
        )

        assert result.ok is True
        assert result.text == '{"notes":[]}'
        assert mock_run.call_count == 5

    @patch("memento.llm.time.sleep")
    @patch("memento.llm.Path.read_text", return_value="")
    @patch("memento.llm.Path.unlink")
    @patch("memento.llm.subprocess.run")
    def test_codex_backend_sleeps_between_failed_attempts(self, mock_run, mock_unlink, mock_read, mock_sleep):
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=0, stdout='{"notes":[]}\n', stderr=""),
        ]

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "codex",
                "llm_model": "gpt-5",
            },
        )

        assert result.ok is True
        mock_sleep.assert_called_once()

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
    def test_timeout_cleans_up_temp_file(self, mock_run, tmp_path):
        """Regression: timeout must not leak the output temp file."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=30)

        with patch("memento.llm.tempfile.NamedTemporaryFile", wraps=None) as mock_tmp:
            # Create real temp files in tmp_path so we can verify cleanup
            call_count = [0]

            def fake_tmpfile(**kwargs):
                call_count[0] += 1
                path = tmp_path / f"memento-llm-{call_count[0]}.txt"
                path.touch()
                handle = MagicMock()
                handle.name = str(path)
                handle.__enter__ = MagicMock(return_value=handle)
                handle.__exit__ = MagicMock(return_value=False)
                return handle

            mock_tmp.side_effect = fake_tmpfile
            result = llm_complete("prompt", {"llm_backend": "codex", "llm_model": "gpt-5"})

        assert result.ok is False
        leftover = list(tmp_path.glob("memento-llm-*.txt"))
        assert len(leftover) == 0, f"Leaked temp files: {[f.name for f in leftover]}"

    @patch("memento.llm.subprocess.run")
    def test_file_not_found_cleans_up_temp_file(self, mock_run, tmp_path):
        """Regression: FileNotFoundError must not leak the output temp file."""
        mock_run.side_effect = FileNotFoundError("codex not found")

        call_count = [0]

        def fake_tmpfile(**kwargs):
            call_count[0] += 1
            path = tmp_path / f"memento-llm-{call_count[0]}.txt"
            path.touch()
            handle = MagicMock()
            handle.name = str(path)
            handle.__enter__ = MagicMock(return_value=handle)
            handle.__exit__ = MagicMock(return_value=False)
            return handle

        with patch("memento.llm.tempfile.NamedTemporaryFile", side_effect=fake_tmpfile):
            result = llm_complete("prompt", {"llm_backend": "codex", "llm_model": "gpt-5"})

        assert result.ok is False
        leftover = list(tmp_path.glob("memento-llm-*.txt"))
        assert len(leftover) == 0, f"Leaked temp files: {[f.name for f in leftover]}"

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

    @patch("memento.llm.Path.read_text", return_value="ok\n")
    @patch("memento.llm.Path.unlink")
    @patch("memento.llm.subprocess.run")
    def test_agent_model_does_not_leak_into_codex(self, mock_run, mock_unlink, mock_read):
        """agent_model (a claude model name) must not be passed to codex as --model."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Global config has agent_model=sonnet (claude name). Caller selects
        # codex via the overriding config dict without setting llm_model.
        with patch(
            "memento.llm.get_config",
            return_value={"llm_backend": "claude", "agent_model": "sonnet"},
        ):
            result = llm_complete("prompt", {"llm_backend": "codex", "llm_model": None})

        cmd = mock_run.call_args[0][0]
        assert "--model" not in cmd
        assert "sonnet" not in cmd
        assert result.ok is True

    @patch("memento.llm.Path.read_text", return_value="ok\n")
    @patch("memento.llm.Path.unlink")
    @patch("memento.llm.subprocess.run")
    def test_explicit_llm_model_still_passes_to_codex(self, mock_run, mock_unlink, mock_read):
        """When llm_model is set explicitly for codex, it passes through."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch("memento.llm.get_config", return_value={"agent_model": "sonnet"}):
            result = llm_complete("prompt", {"llm_backend": "codex", "llm_model": "gpt-5"})

        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        assert "gpt-5" in cmd
        assert "sonnet" not in cmd
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
