"""Shared LLM backend abstraction for hooks."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib import request

from memento.config import get_config


@dataclass(frozen=True)
class LLMResult:
    text: str
    ok: bool
    error: str | None = None
    # Telemetry, populated by llm_complete: lets callers record which
    # backend/model handled the prompt and at what cost without re-deriving
    # config (failures in detached hooks are otherwise unattributable).
    backend: str | None = None
    model: str | None = None
    prompt_bytes: int | None = None
    duration_ms: int | None = None

    def __post_init__(self):
        if self.ok and not self.text:
            raise ValueError("LLMResult: ok=True requires non-empty text")
        if not self.ok and self.error is None:
            raise ValueError("LLMResult: ok=False requires an error message")


def _resolved_config(config=None):
    merged = dict(get_config())
    if config:
        merged.update(config)
    if merged.get("llm_model") is None:
        # `agent_model` predates multi-backend support and holds a claude
        # model name (sonnet/opus/haiku). Only fall back to it for the
        # claude backend — passing it to codex/gemini causes the provider
        # to reject the model and silently return no output.
        if merged.get("llm_backend") == "claude":
            merged["llm_model"] = merged.get("agent_model")
    return merged


def _error(message):
    return LLMResult(text="", ok=False, error=message)


def is_invalid_mcp_config_error(message):
    """Return True when a CLI error looks like Claude rejecting MCP config schema."""
    normalized = (message or "").lower()
    return "invalid mcp configuration" in normalized or ("mcpservers" in normalized and "schema" in normalized)


def _with_invalid_mcp_config_hint(message):
    if not is_invalid_mcp_config_error(message):
        return message
    hint = (
        "Memento hint: Claude rejected the headless MCP config; this is often caused by "
        "a stale headless Claude MCP config in installed hooks. Rerun ./install.sh --reinstall; "
        "if using copied hooks, ensure installed memento/llm.py passes "
        '{"mcpServers": {}} to --mcp-config, not {}.'
    )
    if hint in message:
        return message
    return f"{message}\n\n{hint}"


def _success(text):
    stripped = text.strip()
    if not stripped:
        return _error("LLM returned empty response")
    return LLMResult(text=stripped, ok=True, error=None)


def _stderr_is_warning_only(stderr_text):
    """True when every non-empty stderr line is a warning, not an error."""
    lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
    return bool(lines) and all(line.lower().startswith(("warn", "[warn")) for line in lines)


def _failure_message(result):
    """Build an error message from a failed CLI run, preferring real signal.

    The claude CLI prints some hard failures (e.g. "Prompt is too long") to
    stdout while stderr carries only harmless warnings — dropping stdout here
    hides the real failure reason from triage health logs.
    """
    stderr_text = result.stderr.strip()
    stdout_text = result.stdout.strip()
    if stderr_text and not _stderr_is_warning_only(stderr_text):
        return stderr_text
    if stdout_text and stderr_text:
        return f"{stdout_text}\n[stderr] {stderr_text}"
    if stdout_text:
        return stdout_text
    return stderr_text or f"LLM command failed with exit code {result.returncode}"


def _run_cli(cmd, output_path=None, timeout=30, stdin_input=None):
    try:
        if stdin_input is None:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin_input)
    except subprocess.TimeoutExpired:
        if output_path:
            output_path.unlink(missing_ok=True)
        return _error("LLM command timed out")
    except FileNotFoundError as exc:
        if output_path:
            output_path.unlink(missing_ok=True)
        return _error(str(exc))
    except OSError as exc:
        if output_path:
            output_path.unlink(missing_ok=True)
        return _error(str(exc))

    if result.returncode != 0:
        if output_path is not None:
            try:
                text = output_path.read_text()
            except FileNotFoundError:
                text = ""
            finally:
                try:
                    output_path.unlink()
                except FileNotFoundError:
                    pass
            if text.strip():
                return _success(text)
            if result.stdout.strip():
                return _success(result.stdout)
        return _error(_with_invalid_mcp_config_hint(_failure_message(result)))

    if output_path is not None:
        try:
            text = output_path.read_text()
        finally:
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass
        if not text.strip() and result.stdout.strip():
            return _success(result.stdout)
        return _success(text)

    return _success(result.stdout)


CLAUDE_HEADLESS_DISALLOWED_TOOLS = "Bash,Edit,Write,NotebookEdit,Task,Agent,WebFetch,WebSearch"
CLAUDE_EMPTY_MCP_CONFIG = '{"mcpServers": {}}'


def _resolve_cli_binary(binary):
    resolved = shutil.which(binary)
    if resolved:
        return resolved

    candidates = []
    if binary == "claude":
        candidates.extend(
            [
                Path.home() / ".local" / "bin" / "claude",
                Path("/opt/homebrew/bin/claude"),
                Path("/usr/local/bin/claude"),
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return binary


def _claude_complete(prompt, model=None, timeout=30):
    # Pass the prompt over stdin instead of argv. Large transcripts (>~2MB)
    # overflow ARG_MAX and raise OSError("Argument list too long"); stdin has
    # no such ceiling.
    # Headless memento prompts are text-in/JSON-out. Do not inherit a user's
    # interactive auto-permission toolbelt: SessionEnd runs detached after the
    # human has left, so tool side effects would be surprising and hard to stop.
    # Disable built-in tools and inherited MCP servers, with a denylist as
    # defense-in-depth for Claude Code versions that still expose tools in
    # --print mode despite a tighter tool configuration.
    cmd = [
        _resolve_cli_binary("claude"),
        "--print",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        CLAUDE_EMPTY_MCP_CONFIG,
        "--permission-mode",
        "default",
        "--disallowedTools",
        CLAUDE_HEADLESS_DISALLOWED_TOOLS,
    ]
    if model:
        cmd.extend(["--model", model])
    return _run_cli(cmd, stdin_input=prompt, timeout=timeout)


def _codex_complete(prompt, model=None, timeout=30):
    errors = []
    for attempt in range(5):
        with tempfile.NamedTemporaryFile(prefix="memento-llm-", suffix=".txt", delete=False) as handle:
            output_path = Path(handle.name)

        # Pass the prompt over stdin ("-" sentinel), not argv: rendered
        # transcripts can exceed ARG_MAX (exactly 1MB on macOS), which makes
        # exec fail before codex even starts.
        cmd = ["codex", "exec", "--ephemeral"]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(["-o", str(output_path), "-"])
        result = _run_cli(cmd, output_path=output_path, timeout=timeout, stdin_input=prompt)
        if result.ok:
            return result
        errors.append(result.error or "unknown")
        # Don't retry non-transient errors
        if result.error and ("not found" in result.error.lower() or "auth" in result.error.lower()):
            return result
        if attempt < 4:
            time.sleep(1)

    return _error(f"codex failed after 5 attempts. Last: {errors[-1]}")


def _gemini_complete(prompt, model=None, timeout=30):
    # Same ARG_MAX hazard as codex: keep the prompt off argv.
    cmd = ["gemini"]
    if model:
        cmd.extend(["--model", model])
    return _run_cli(cmd, timeout=timeout, stdin_input=prompt)


def _api_complete(url, headers, payload, extract_text, timeout=30):
    from urllib.error import HTTPError, URLError

    data = json.dumps(payload).encode()
    req = request.Request(url, data=data, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
        req.headers[key] = value
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read().decode())
    except (URLError, HTTPError, OSError, json.JSONDecodeError) as exc:
        return _error(str(exc))

    try:
        return _success(extract_text(body))
    except (KeyError, TypeError, IndexError) as exc:
        return _error(f"Unexpected LLM response structure: {exc}")


def _anthropic_api_complete(prompt, model, api_key, timeout=30):
    return _api_complete(
        "https://api.anthropic.com/v1/messages",
        {
            "content-type": "application/json",
            "x-api-key": api_key or "",
            "anthropic-version": "2023-06-01",
        },
        {
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        },
        lambda body: "".join(part.get("text", "") for part in body.get("content", []) if part.get("type") == "text"),
        timeout=timeout,
    )


def _openai_compat_complete(prompt, model, api_key, base_url, timeout=30):
    base = (base_url or "https://api.openai.com/v1").rstrip("/")
    return _api_complete(
        f"{base}/chat/completions",
        {
            "content-type": "application/json",
            "authorization": f"Bearer {api_key or ''}",
        },
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        },
        lambda body: ((body.get("choices") or [{}])[0].get("message") or {}).get("content", ""),
        timeout=timeout,
    )


def llm_complete(prompt, config=None, timeout=None):
    resolved = _resolved_config(config)
    backend = resolved.get("llm_backend", "claude")
    model = resolved.get("llm_model")
    prompt_bytes = len(prompt.encode("utf-8"))
    # Scale timeout with prompt size for every backend. Baseline 60s covers
    # short completions; add 1s per 5KB of prompt so a 500KB transcript gets
    # ~160s, capped at 300s.
    effective_timeout = timeout if timeout is not None else max(60, min(300, 60 + prompt_bytes // 5_000))

    started = time.time()
    if backend == "claude":
        result = _claude_complete(prompt, model, timeout=effective_timeout)
    elif backend == "codex":
        result = _codex_complete(prompt, model, timeout=effective_timeout)
    elif backend == "gemini":
        result = _gemini_complete(prompt, model, timeout=effective_timeout)
    elif backend == "anthropic-api":
        result = _anthropic_api_complete(prompt, model, resolved.get("llm_api_key"), timeout=effective_timeout)
    elif backend == "openai-compat":
        result = _openai_compat_complete(
            prompt, model, resolved.get("llm_api_key"), resolved.get("llm_api_base"), timeout=effective_timeout
        )
    else:
        result = _error(f"Unknown LLM backend: {backend}")

    return replace(
        result,
        backend=backend,
        model=model,
        prompt_bytes=prompt_bytes,
        duration_ms=int((time.time() - started) * 1000),
    )


def preflight_check(config=None):
    resolved = _resolved_config(config)
    backend = resolved.get("llm_backend", "claude")

    if backend in {"claude", "codex", "gemini"}:
        binary = {"claude": "claude", "codex": "codex", "gemini": "gemini"}[backend]
        try:
            result = subprocess.run(
                [_resolve_cli_binary(binary), "--version"], capture_output=True, text=True, timeout=10
            )
        except subprocess.TimeoutExpired:
            return False, f"{binary} preflight timed out"
        except FileNotFoundError as exc:
            return False, str(exc)
        if result.returncode != 0:
            return False, result.stderr.strip() or f"{binary} preflight failed"
        return True, f"{binary} available"

    if backend == "anthropic-api":
        if resolved.get("llm_api_key"):
            return True, "anthropic api key configured"
        return False, "Missing llm_api_key for anthropic-api backend"

    if backend == "openai-compat":
        if not resolved.get("llm_api_key"):
            return False, "Missing llm_api_key for openai-compat backend"
        return True, "openai-compatible api configured"

    return False, f"Unknown LLM backend: {backend}"
