"""Tests for the shared LLM backend abstraction."""

import io
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

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
        assert cmd[0].endswith("claude")
        assert cmd[1:11] == [
            "--print",
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers": {}}',
            "--permission-mode",
            "default",
            "--disallowedTools",
            "Bash,Edit,Write,NotebookEdit,Task,Agent,WebFetch,WebSearch",
        ]
        assert cmd[-2:] == ["--model", "sonnet"]
        assert mock_run.call_args.kwargs["input"] == "test prompt"
        assert result.ok is True
        assert result.text == "claude output"

    @patch("memento.llm.subprocess.run")
    def test_claude_backend_sandboxes_headless_spawn(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}\n", stderr="")

        llm_complete("transcript", {"llm_backend": "claude"})

        cmd = mock_run.call_args[0][0]
        assert "--tools" in cmd
        assert cmd[cmd.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in cmd
        assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers": {}}'
        assert "{}" not in cmd
        assert "--permission-mode" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "default"
        denylist = cmd[cmd.index("--disallowedTools") + 1].split(",")
        for tool in ["Bash", "Edit", "Write", "NotebookEdit", "Task", "Agent", "WebFetch", "WebSearch"]:
            assert tool in denylist
        # MultiEdit no longer exists in Claude Code; keeping it in the denylist
        # makes the CLI print a warning on every headless run, which pollutes
        # stderr and gets misrecorded as the failure reason in health logs.
        assert "MultiEdit" not in denylist

    @patch("memento.llm.subprocess.run")
    def test_claude_backend_can_enable_bare_headless_mode(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}\n", stderr="")

        llm_complete(
            "transcript",
            {
                "llm_backend": "claude",
                "claude_bare_headless": True,
            },
        )

        cmd = mock_run.call_args[0][0]
        assert "--bare" in cmd
        assert cmd[cmd.index("--bare") + 1] == "--tools"
        assert cmd[cmd.index("--permission-mode") + 1] == "default"
        assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers": {}}'

    @patch("memento.llm.subprocess.run")
    def test_claude_backend_adds_actionable_invalid_mcp_hint(self, mock_run):
        stderr = "Error: Invalid MCP configuration:\nmcpServers: Does not adhere to MCP server configuration schema"
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr=stderr)

        result = llm_complete("transcript", {"llm_backend": "claude"})

        assert result.ok is False
        assert stderr in result.error
        assert "stale headless Claude MCP config" in result.error
        assert "./install.sh --reinstall" in result.error
        assert '{"mcpServers": {}}' in result.error

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
        # Prompt travels over stdin ("-" sentinel), never argv: rendered
        # transcripts can exceed ARG_MAX (1MB on macOS).
        assert cmd[-1] == "-"
        assert "test prompt" not in cmd
        assert mock_run.call_args.kwargs["input"] == "test prompt"
        assert result.ok is True
        assert result.text == "codex output"
        mock_unlink.assert_called_once()
        mock_read.assert_called_once()

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
        assert cmd == ["gemini", "--model", "gemini-2.5-pro"]
        assert mock_run.call_args.kwargs["input"] == "test prompt"
        assert result.ok is True
        assert result.text == "gemini output"

    @patch("memento.llm.Path.read_text", return_value="ok\n")
    @patch("memento.llm.Path.unlink")
    @patch("memento.llm.subprocess.run")
    def test_codex_backend_receives_scaled_timeout(self, mock_run, mock_unlink, mock_read):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        prompt = "x" * 500_000  # ~160s scaled timeout, not _run_cli's 30s default

        llm_complete(prompt, {"llm_backend": "codex"})

        assert mock_run.call_args.kwargs["timeout"] == 160

    @patch("memento.llm.subprocess.run")
    def test_gemini_backend_receives_scaled_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
        prompt = "x" * 500_000

        llm_complete(prompt, {"llm_backend": "gemini"})

        assert mock_run.call_args.kwargs["timeout"] == 160

    @patch("memento.llm.subprocess.run")
    def test_pi_backend_builds_correct_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="pi output\n", stderr="")

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "pi",
                "llm_model": "openrouter/deepseek/deepseek-v4-pro",
            },
        )

        cmd = mock_run.call_args[0][0]
        assert cmd[0:3] == ["pi", "--print", "--mode"]
        assert "--no-tools" in cmd
        assert "--no-session" in cmd
        assert "--no-extensions" in cmd
        assert cmd[-2:] == ["--model", "openrouter/deepseek/deepseek-v4-pro"]
        # Prompt travels over stdin, never argv (same ARG_MAX hazard as codex/gemini).
        assert "test prompt" not in cmd
        assert mock_run.call_args.kwargs["input"] == "test prompt"
        assert result.ok is True
        assert result.text == "pi output"

    @patch("memento.llm.subprocess.run")
    def test_pi_backend_omits_model_flag_when_unset(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="pi output\n", stderr="")

        llm_complete("test prompt", {"llm_backend": "pi"})

        cmd = mock_run.call_args[0][0]
        assert "--model" not in cmd

    @patch("memento.llm.subprocess.run")
    def test_pi_backend_receives_scaled_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
        prompt = "x" * 500_000

        llm_complete(prompt, {"llm_backend": "pi"})

        assert mock_run.call_args.kwargs["timeout"] == 160

    @patch("memento.llm.subprocess.run")
    def test_pi_backend_returns_error_on_missing_binary(self, mock_run):
        mock_run.side_effect = FileNotFoundError("pi not found")

        result = llm_complete("prompt", {"llm_backend": "pi"})

        assert result.ok is False
        assert "not found" in result.error.lower()

    @patch("memento.llm.subprocess.run")
    def test_pi_backend_returns_error_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="pi", timeout=30)

        result = llm_complete("prompt", {"llm_backend": "pi"})

        assert result.ok is False
        assert "timed out" in result.error.lower()

    @patch("memento.llm.subprocess.run")
    def test_pi_backend_surfaces_nonzero_exit_stderr(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Error: Model not found")

        result = llm_complete("prompt", {"llm_backend": "pi"})

        assert result.ok is False
        assert "Model not found" in result.error

    @patch("memento.llm.subprocess.run")
    def test_preflight_check_pi(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="0.80.2\n", stderr="")

        ok, message = preflight_check({"llm_backend": "pi"})

        assert ok is True
        assert "pi" in message.lower()
        cmd = mock_run.call_args[0][0]
        assert cmd[0].endswith("pi")
        assert cmd[1:] == ["--version"]

    @patch("memento.llm.subprocess.run")
    def test_preflight_check_pi_missing_binary_is_clear_not_a_crash(self, mock_run):
        mock_run.side_effect = FileNotFoundError("[Errno 2] No such file or directory: 'pi'")

        ok, message = preflight_check({"llm_backend": "pi"})

        assert ok is False
        assert "pi" in message.lower()

    @patch("memento.llm.subprocess.run")
    def test_llm_complete_attaches_telemetry_on_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="claude output\n", stderr="")

        result = llm_complete("test prompt", {"llm_backend": "claude", "llm_model": "sonnet"})

        assert result.backend == "claude"
        assert result.model == "sonnet"
        assert result.prompt_bytes == len("test prompt".encode("utf-8"))
        assert result.output_bytes == len("claude output".encode("utf-8"))
        assert result.duration_ms is not None and result.duration_ms >= 0

    @patch("memento.llm.subprocess.run")
    def test_llm_complete_attaches_telemetry_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="Prompt is too long\n", stderr="")

        result = llm_complete("test prompt", {"llm_backend": "claude"})

        assert result.ok is False
        assert result.backend == "claude"
        assert result.prompt_bytes == len("test prompt".encode("utf-8"))
        assert result.output_bytes == 0
        assert result.duration_ms is not None

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
    def test_error_surfaces_stdout_when_stderr_empty(self, mock_run):
        # The claude CLI prints "Prompt is too long" to stdout and exits 1
        # with an empty stderr; the real reason must reach the caller.
        mock_run.return_value = MagicMock(returncode=1, stdout="Prompt is too long\n", stderr="")

        result = llm_complete("prompt", {"llm_backend": "claude"})

        assert result.ok is False
        assert "Prompt is too long" in result.error

    @patch("memento.llm.subprocess.run")
    def test_error_surfaces_stdout_when_stderr_is_warning_only(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="Prompt is too long\n",
            stderr="Warning: unknown tool in disallowedTools\n",
        )

        result = llm_complete("prompt", {"llm_backend": "claude"})

        assert result.ok is False
        assert "Prompt is too long" in result.error
        assert "unknown tool in disallowedTools" in result.error

    @patch("memento.llm.subprocess.run")
    def test_error_prefers_real_stderr_over_stdout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="partial output\n", stderr="fatal: real error\n")

        result = llm_complete("prompt", {"llm_backend": "claude"})

        assert result.ok is False
        assert result.error == "fatal: real error"

    @patch("memento.llm.subprocess.run")
    def test_get_backend_from_config(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")

        with patch("memento.llm.get_config", return_value={"llm_backend": "claude", "agent_model": "haiku"}):
            result = llm_complete("prompt")

        cmd = mock_run.call_args[0][0]
        assert cmd[0].endswith("claude")
        assert cmd[1:11] == [
            "--print",
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers": {}}',
            "--permission-mode",
            "default",
            "--disallowedTools",
            "Bash,Edit,Write,NotebookEdit,Task,Agent,WebFetch,WebSearch",
        ]
        assert cmd[-2:] == ["--model", "haiku"]
        assert mock_run.call_args.kwargs["input"] == "prompt"
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
        assert cmd[0].endswith("claude")
        assert cmd[1:] == ["--version"]

    @patch("memento.llm.Path.exists")
    @patch("memento.llm.shutil.which", return_value=None)
    @patch("memento.llm.subprocess.run")
    def test_claude_backend_falls_back_to_user_local_binary(self, mock_run, _which, mock_exists):
        mock_exists.side_effect = lambda: True
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")

        with patch("memento.llm.Path.home", return_value=Path("/home/user")):
            result = llm_complete("prompt", {"llm_backend": "claude"})

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/home/user/.local/bin/claude"
        assert result.ok is True

    @patch("memento.llm.Path.exists")
    @patch("memento.llm.shutil.which", return_value=None)
    @patch("memento.llm.subprocess.run")
    def test_preflight_check_claude_uses_user_local_binary(self, mock_run, _which, mock_exists):
        mock_exists.side_effect = lambda: True
        mock_run.return_value = MagicMock(returncode=0, stdout="1.0.0\n", stderr="")

        with patch("memento.llm.Path.home", return_value=Path("/home/user")):
            ok, _message = preflight_check({"llm_backend": "claude"})

        assert ok is True
        assert mock_run.call_args[0][0] == ["/home/user/.local/bin/claude", "--version"]


class TestApiBackends:
    @patch("memento.llm.request.urlopen")
    def test_anthropic_api_backend_sends_correct_request(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({"content": [{"type": "text", "text": "anthropic output"}]}).encode()
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
        assert payload["max_tokens"] == 4096
        assert result.ok is True
        assert result.text == "anthropic output"
        assert result.output_bytes == len("anthropic output".encode("utf-8"))

    @patch("memento.llm.request.urlopen")
    def test_anthropic_api_backend_uses_configurable_max_tokens_and_scaled_timeout(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode()
        mock_urlopen.return_value.__enter__.return_value = response

        llm_complete(
            "x" * 500_000,
            {
                "llm_backend": "anthropic-api",
                "llm_model": "claude-3-5-sonnet-latest",
                "llm_api_key": "secret",
                "llm_max_tokens": 1234,
                "llm_api_retries": 1,
            },
        )

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode())
        assert payload["max_tokens"] == 1234
        assert mock_urlopen.call_args.kwargs["timeout"] == 160

    @patch("memento.llm.time.sleep")
    @patch("memento.llm.request.urlopen")
    def test_anthropic_api_retries_retryable_http_failure(self, mock_urlopen, mock_sleep):
        response = MagicMock()
        response.read.return_value = json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode()
        response_context = MagicMock()
        response_context.__enter__.return_value = response
        retryable = HTTPError(
            "https://api.anthropic.com/v1/messages",
            429,
            "rate limited",
            {"Retry-After": "0"},
            io.BytesIO(b"rate limited"),
        )
        mock_urlopen.side_effect = [retryable, response_context]

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "anthropic-api",
                "llm_model": "claude-3-5-sonnet-latest",
                "llm_api_key": "secret",
                "llm_api_retries": 2,
            },
        )

        assert result.ok is True
        assert result.text == "ok"
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once_with(0.0)

    @patch("memento.llm.time.sleep")
    @patch("memento.llm.request.urlopen")
    def test_anthropic_api_retries_transient_network_failure(self, mock_urlopen, mock_sleep):
        response = MagicMock()
        response.read.return_value = json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode()
        response_context = MagicMock()
        response_context.__enter__.return_value = response
        mock_urlopen.side_effect = [URLError("connection reset"), response_context]

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "anthropic-api",
                "llm_model": "claude-3-5-sonnet-latest",
                "llm_api_key": "secret",
                "llm_api_retries": 2,
                "llm_api_initial_backoff_seconds": 0,
            },
        )

        assert result.ok is True
        assert result.text == "ok"
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once_with(0.0)

    @patch("memento.llm.time.sleep")
    @patch("memento.llm.request.urlopen")
    def test_anthropic_api_does_not_retry_non_retryable_http_failure(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = HTTPError(
            "https://api.anthropic.com/v1/messages",
            400,
            "bad request",
            {},
            io.BytesIO(b"bad request"),
        )

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "anthropic-api",
                "llm_model": "claude-3-5-sonnet-latest",
                "llm_api_key": "secret",
                "llm_api_retries": 3,
            },
        )

        assert result.ok is False
        assert "HTTP 400" in result.error
        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called()

    @patch("memento.llm.request.urlopen")
    def test_anthropic_api_backend_uses_tool_choice_for_structured_json(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "content": [{"type": "tool_use", "name": "emit_notes", "input": {"notes": []}}],
                "usage": {"input_tokens": 11, "output_tokens": 22},
            }
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = response

        result = llm_complete(
            "test prompt",
            {
                "llm_backend": "anthropic-api",
                "llm_model": "claude-3-5-sonnet-latest",
                "llm_api_key": "secret",
                "llm_structured_json_tool_name": "emit_notes",
                "llm_structured_json_schema": {"type": "object", "properties": {"notes": {"type": "array"}}},
            },
        )

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode())
        assert payload["tool_choice"] == {"type": "tool", "name": "emit_notes"}
        assert payload["tools"][0]["input_schema"]["properties"]["notes"]["type"] == "array"
        assert json.loads(result.text) == {"notes": []}
        assert result.input_tokens == 11
        assert result.output_tokens == 22
        assert result.output_bytes == len(result.text.encode("utf-8"))

    @patch("memento.llm.request.urlopen")
    def test_openai_compat_backend_sends_correct_request(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({"choices": [{"message": {"content": "openai output"}}]}).encode()
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
