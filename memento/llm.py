"""Shared LLM backend abstraction for hooks."""

from dataclasses import dataclass
import json
from pathlib import Path
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


def _success(text):
    stripped = text.strip()
    if not stripped:
        return _error("LLM returned empty response")
    return LLMResult(text=stripped, ok=True, error=None)


def _run_cli(cmd, output_path=None, timeout=30, stdin_input=None):
    try:
        if stdin_input is None:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
            )
        else:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, input=stdin_input
            )
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
        return _error(result.stderr.strip() or f"LLM command failed with exit code {result.returncode}")

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


def _claude_complete(prompt, model=None, timeout=30):
    # Pass the prompt over stdin instead of argv. Large transcripts (>~2MB)
    # overflow ARG_MAX and raise OSError("Argument list too long"); stdin has
    # no such ceiling.
    cmd = ["claude", "--print"]
    if model:
        cmd.extend(["--model", model])
    return _run_cli(cmd, stdin_input=prompt, timeout=timeout)


def _codex_complete(prompt, model=None):
    errors = []
    for attempt in range(5):
        with tempfile.NamedTemporaryFile(prefix="memento-llm-", suffix=".txt", delete=False) as handle:
            output_path = Path(handle.name)

        cmd = ["codex", "exec", "--ephemeral"]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(["-o", str(output_path), prompt])
        result = _run_cli(cmd, output_path=output_path)
        if result.ok:
            return result
        errors.append(result.error or "unknown")
        # Don't retry non-transient errors
        if result.error and ("not found" in result.error.lower() or "auth" in result.error.lower()):
            return result
        if attempt < 4:
            time.sleep(1)

    return _error(f"codex failed after 5 attempts. Last: {errors[-1]}")


def _gemini_complete(prompt, model=None):
    cmd = ["gemini"]
    if model:
        cmd.extend(["--model", model])
    cmd.extend(["-p", prompt])
    return _run_cli(cmd)


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


def _anthropic_api_complete(prompt, model, api_key):
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
    )


def _openai_compat_complete(prompt, model, api_key, base_url):
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
    )


def llm_complete(prompt, config=None, timeout=None):
    resolved = _resolved_config(config)
    backend = resolved.get("llm_backend", "claude")
    model = resolved.get("llm_model")
    # Scale timeout with prompt size. Baseline 60s covers short completions;
    # add 1s per 5KB of prompt so a 500KB transcript gets ~160s, capped at 300s.
    effective_timeout = (
        timeout if timeout is not None else max(60, min(300, 60 + len(prompt) // 5_000))
    )

    if backend == "claude":
        return _claude_complete(prompt, model, timeout=effective_timeout)
    if backend == "codex":
        return _codex_complete(prompt, model)
    if backend == "gemini":
        return _gemini_complete(prompt, model)
    if backend == "anthropic-api":
        return _anthropic_api_complete(prompt, model, resolved.get("llm_api_key"))
    if backend == "openai-compat":
        return _openai_compat_complete(prompt, model, resolved.get("llm_api_key"), resolved.get("llm_api_base"))

    return _error(f"Unknown LLM backend: {backend}")


def preflight_check(config=None):
    resolved = _resolved_config(config)
    backend = resolved.get("llm_backend", "claude")

    if backend in {"claude", "codex", "gemini"}:
        binary = {"claude": "claude", "codex": "codex", "gemini": "gemini"}[backend]
        try:
            result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
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
