"""Shared lifecycle retrieval primitives for memento host adapters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess as _subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from memento.config import RUNTIME_DIR, detect_project, get_config, get_vault, slugify
from memento.graph import load_or_build_graph, lookup_concepts, lookup_project_notes, read_note_metadata
from memento import telemetry
from memento.health import build_automation_memory_readiness
from memento.llm import is_invalid_mcp_config_error, llm_complete
from memento.retrieval_policy import PromptRecallRequest, PromptRecallRuntime
from memento.search import (
    MISS_RECOVERY_HINTS,
    build_search_miss,
    enhance_results,
    has_qmd,
    is_vsearch_warm,
    mark_vsearch_warm,
    multi_hop_search,
    normalize_miss_reason,
    prf_expand_query,
    qmd_search,
    qmd_search_with_extras,
    resolve_concrete_mode,
    rrf_fuse,
)
from memento.store import (
    RETRIEVAL_LOG_PATH,
    TRIAGE_HEALTH_LOG_PATH,
    log_automation_memory_health,
    log_retrieval,
    record_access,
)
from memento.utils import read_hook_input

TRIAGE_HEALTH_WINDOW_HOURS = telemetry.HEALTH_WINDOW_HOURS
TRIAGE_HEALTH_MIN_EVENTS = telemetry.HEALTH_MIN_EVENTS_FOR_RATE
TRIAGE_HEALTH_FAIL_RATIO = telemetry.HEALTH_WARN_FAILURE_RATIO
_STALE_CERTAINTY_HINT = (
    "likely stale installed memento package; rerun ./install.sh --reinstall; "
    "current triage accepts certainty labels like confirmed"
)
_ACCEPTED_CERTAINTY_LABELS = {
    "speculation",
    "speculative",
    "uncertain",
    "low",
    "medium",
    "moderate",
    "likely",
    "confirmed",
    "certain",
    "high",
    "proven",
    "verified",
}


@dataclass
class LifecycleResult:
    """Result returned by lifecycle builders and adapted by host integrations."""

    should_inject: bool
    content: str
    source: str
    results: list[dict] = field(default_factory=list)
    reason: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = {
            "should_inject": self.should_inject,
            "content": self.content,
            "source": self.source,
            "results": self.results,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


class StructuredMissReason(str):
    """String reason that carries a structured miss payload through recall internals."""

    def __new__(cls, reason: str, miss: dict):
        obj = str.__new__(cls, reason)
        obj.miss = miss
        return obj


def empty_result(source: str, reason: str = "no-results") -> LifecycleResult:
    return LifecycleResult(
        should_inject=False,
        content="",
        source=source,
        reason=reason,
    )


TRIAGE_HEALTH_SUCCESS_ACTIONS = telemetry.TRIAGE_HEALTH_SUCCESS_ACTIONS
TRIAGE_HEALTH_FAILURE_ACTIONS = telemetry.TRIAGE_HEALTH_FAILURE_ACTIONS


def _is_stale_certainty_error(error):
    text = str(error or "")
    if "invalid literal for int()" not in text:
        return False
    return any(f"'{label}'" in text or f'"{label}"' in text for label in _ACCEPTED_CERTAINTY_LABELS)


def _scan_triage_health_log(path, cutoff, mode="health"):
    if not os.path.exists(path):
        return 0, 0, False, False, ""

    total = 0
    failed = 0
    invalid_mcp_failed = False
    stale_certainty_failed = False
    last_error = ""
    for rec in telemetry.iter_recent_jsonl(Path(path), cutoff):
        if rec.get("hook") != "triage":
            continue
        action = rec.get("action") or ""
        if mode == "legacy":
            if action not in ("decision", "parse_transcript_failed", "structured_notes_llm_failed"):
                continue
            total += 1
            if action != "decision":
                failed += 1
                error = rec.get("error", "")
                if error:
                    last_error = error
                invalid_mcp_failed = invalid_mcp_failed or is_invalid_mcp_config_error(error)
                stale_certainty_failed = stale_certainty_failed or _is_stale_certainty_error(error)
            continue

        if action in TRIAGE_HEALTH_SUCCESS_ACTIONS:
            total += 1
        elif action in TRIAGE_HEALTH_FAILURE_ACTIONS:
            total += 1
            failed += 1
            error = rec.get("error", "")
            if error:
                last_error = error
            invalid_mcp_failed = invalid_mcp_failed or is_invalid_mcp_config_error(error)
            stale_certainty_failed = stale_certainty_failed or _is_stale_certainty_error(error)
    return total, failed, invalid_mcp_failed, stale_certainty_failed, last_error


TRIAGE_WARN_STATE_PATH = os.path.join(RUNTIME_DIR, "triage-warn-state.json")


def _triage_warn_shown_today():
    try:
        with open(TRIAGE_WARN_STATE_PATH) as f:
            return json.load(f).get("date") == datetime.now().strftime("%Y-%m-%d")
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def _mark_triage_warn_shown():
    try:
        os.makedirs(os.path.dirname(TRIAGE_WARN_STATE_PATH), exist_ok=True)
        with open(TRIAGE_WARN_STATE_PATH, "w") as f:
            json.dump({"date": datetime.now().strftime("%Y-%m-%d")}, f)
    except OSError:
        pass


PI_BRIDGE_WARN_STATE_PATH = os.path.join(RUNTIME_DIR, "pi-bridge-warn-state.json")


def _pi_bridge_warn_shown_today():
    try:
        with open(PI_BRIDGE_WARN_STATE_PATH) as f:
            return json.load(f).get("date") == datetime.now().strftime("%Y-%m-%d")
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def _mark_pi_bridge_warn_shown():
    try:
        os.makedirs(os.path.dirname(PI_BRIDGE_WARN_STATE_PATH), exist_ok=True)
        with open(PI_BRIDGE_WARN_STATE_PATH, "w") as f:
            json.dump({"date": datetime.now().strftime("%Y-%m-%d")}, f)
    except OSError:
        pass


def _health_error_excerpt(error: object, limit: int = 140) -> str:
    text = telemetry.redact_text(" ".join(str(error or "").split()))
    text = _strip_injection(text)
    return text[:limit]


_PI_BRIDGE_FAILURE_ACTIONS = telemetry.PI_BRIDGE_FAILURE_ACTIONS


def _is_pi_bridge_failure_record(rec: dict[str, object]) -> bool:
    return telemetry.is_pi_bridge_failure_record(rec)


def _parse_health_timestamp(raw: object) -> datetime | None:
    return telemetry.parse_timestamp_naive_utc(raw)


def _scan_pi_bridge_health_log(path: str, cutoff: datetime) -> tuple[str | None, int, dict[str, object] | None]:
    if not os.path.exists(path):
        return str(path), 0, None

    count = 0
    latest: dict[str, object] | None = None
    latest_ts: datetime | None = None
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("hook") != "pi-bridge":
                continue
            ts_raw = rec.get("ts")
            if not ts_raw:
                continue
            ts = _parse_health_timestamp(ts_raw)
            if ts is None:
                continue
            if ts < cutoff or not _is_pi_bridge_failure_record(rec):
                continue
            count += 1
            if latest_ts is None or ts >= latest_ts:
                latest_ts = ts
                latest = rec
    return str(path), count, latest


def pi_bridge_health_warning(rate_limited=False):
    """Return a one-line warning if recent Pi bridge commands are failing."""
    try:
        cutoff = datetime.now() - timedelta(hours=TRIAGE_HEALTH_WINDOW_HOURS)
        log_path, failures, latest = _scan_pi_bridge_health_log(TRIAGE_HEALTH_LOG_PATH, cutoff)
        if failures == 0:
            return None
        if rate_limited:
            if _pi_bridge_warn_shown_today():
                return None
            _mark_pi_bridge_warn_shown()

        warning = (
            f"[vault] WARN: Pi bridge failing {failures} recent command(s) in last {TRIAGE_HEALTH_WINDOW_HOURS}h "
            f"— check {log_path}"
        )
        if latest:
            operation = str(latest.get("operation") or latest.get("action") or "unknown")
            backend = str(latest.get("backend") or "unknown")
            project = str(latest.get("project") or "unknown")
            cwd = str(latest.get("cwd") or "unknown")
            session_id = str(latest.get("session_id") or "unknown")
            warning += f" — last: {operation} via {backend} · {project} · {cwd} · session {session_id}"
            if latest.get("error"):
                warning += f' — error: "{_health_error_excerpt(latest.get("error"))}"'
        return warning
    except Exception:
        return None


def triage_health_warning(rate_limited=False):
    """Return a one-line warning if SessionEnd triage is silently failing.

    Scans the always-on triage health log first, falling back to legacy
    retrieval.jsonl diagnostics for older installs. Returns None when healthy
    or when there isn't enough data to judge.

    With rate_limited=True (the injection surfaces: briefing, session
    context), the warning fires at most once per day — it was previously
    re-injected into 100% of session briefings for days at a time. Diagnostic
    surfaces (memento health) pass False and always see it.
    """
    try:
        cutoff = datetime.now() - timedelta(hours=TRIAGE_HEALTH_WINDOW_HOURS)
        log_path = TRIAGE_HEALTH_LOG_PATH
        total, failed, invalid_mcp_failed, stale_certainty_failed, last_error = _scan_triage_health_log(
            log_path, cutoff
        )
        if total < TRIAGE_HEALTH_MIN_EVENTS:
            (
                legacy_total,
                legacy_failed,
                legacy_invalid_mcp_failed,
                legacy_stale_certainty_failed,
                legacy_last_error,
            ) = _scan_triage_health_log(RETRIEVAL_LOG_PATH, cutoff, mode="legacy")
            if legacy_total >= total:
                total = legacy_total
                failed = legacy_failed
                invalid_mcp_failed = legacy_invalid_mcp_failed
                stale_certainty_failed = legacy_stale_certainty_failed
                last_error = legacy_last_error
                log_path = RETRIEVAL_LOG_PATH

        if total < TRIAGE_HEALTH_MIN_EVENTS:
            return None
        if telemetry.failure_rate(failed, total) < TRIAGE_HEALTH_FAIL_RATIO:
            return None
        if rate_limited:
            if _triage_warn_shown_today():
                return None
            _mark_triage_warn_shown()
        warning = (
            f"[vault] WARN: triage failing {failed}/{total} in last {TRIAGE_HEALTH_WINDOW_HOURS}h — check {log_path}"
        )
        if last_error:
            # The warning is injected into prompt-visible briefing content;
            # error text can quote arbitrary LLM/CLI output, so strip
            # instruction-like patterns before embedding it.
            snippet = _strip_injection(" ".join(str(last_error).split()))[:140]
            warning += f' — last error: "{snippet}"'
        if invalid_mcp_failed:
            warning += (
                " — likely stale headless Claude MCP config; rerun ./install.sh --reinstall; "
                'copied hooks should use {"mcpServers": {}} for --mcp-config'
            )
        if stale_certainty_failed:
            warning += f" — {_STALE_CERTAINTY_HINT}"
        return warning
    except Exception:
        return None


def get_git_branch(cwd):
    """Read current git branch from cwd."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def read_project_index(project_slug):
    """Read recent sessions and linked notes from the project index."""
    vault = get_vault()
    project_file = vault / "projects" / f"{project_slug}.md"
    if not project_file.exists():
        return [], []

    content = project_file.read_text()
    lines = content.splitlines()

    sessions = []
    notes = []
    in_sessions = False
    in_notes = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## Sessions") or (
            stripped.startswith("## ") and stripped[3:4].isalpha() and "-" in stripped
        ):
            in_sessions = True
            in_notes = False
            continue
        elif stripped == "## Notes":
            in_notes = True
            in_sessions = False
            continue
        elif stripped.startswith("## "):
            in_sessions = False
            in_notes = False
            continue

        if in_sessions and stripped.startswith("- "):
            sessions.append(stripped[2:])
        elif in_notes and "[[" in stripped:
            for match in re.finditer(r"\[\[([^\]]+)\]\]", stripped):
                notes.append(match.group(1))

    return sessions, notes


def read_note_oneliner(note_name):
    """Read a note's title and certainty from frontmatter."""
    vault = get_vault()
    note_path = vault / "notes" / f"{note_name}.md"
    if not note_path.exists():
        return None

    title = note_name
    certainty = ""
    note_type = ""

    with open(note_path) as f:
        in_frontmatter = False
        for line in f:
            stripped = line.strip()
            if stripped == "---":
                if not in_frontmatter:
                    in_frontmatter = True
                    continue
                else:
                    break
            if in_frontmatter:
                if stripped.startswith("title:"):
                    title = stripped[6:].strip().strip('"').strip("'")
                elif stripped.startswith("certainty:"):
                    certainty = stripped[10:].strip()
                elif stripped.startswith("type:"):
                    note_type = stripped[5:].strip()

    meta_parts = []
    if certainty:
        meta_parts.append(f"certainty:{certainty}")
    if note_type:
        meta_parts.append(note_type)

    meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
    return f"{title}{meta}"


def _strip_injection(text):
    """Strip instruction-like patterns from injected content (defense-in-depth)."""
    if not text:
        return text
    text = re.sub(r"(?i)(ignore\s+(all\s+)?previous\s+instructions)", "[filtered]", text)
    text = re.sub(r"(?i)(you\s+are\s+now\s+|you\s+must\s+now\s+)", "[filtered]", text)
    text = re.sub(r"(?i)^(system|assistant)\s*:", "[filtered]:", text)
    text = re.sub(r"</?s>", "", text)
    return text


def format_qmd_result(result):
    """Format a QMD search result as a one-liner."""
    title = _strip_injection(result.get("title", ""))
    snippet = _strip_injection(result.get("snippet", "").strip())

    # Truncate snippet to first sentence or 100 chars
    if snippet:
        dot = snippet.find(".")
        if 0 < dot < 100:
            snippet = snippet[: dot + 1]
        elif len(snippet) > 100:
            snippet = snippet[:100] + "..."

    parts = [f"  - {title}"]
    if snippet:
        parts[0] += f": {snippet}"
    return parts[0]


def _find_hook_script(name):
    """Locate a worker hook script in both repo and installed layouts.

    Repo layout:      <repo>/memento/lifecycle.py with <repo>/hooks/<name>
    Installed layout: ~/.claude/hooks/memento/lifecycle.py with ~/.claude/hooks/<name>
    """
    base = Path(__file__).resolve().parent.parent
    for candidate in (base / "hooks" / name, base / name):
        if candidate.exists():
            return candidate
    return None


def _safe_scope_part(value: object, default: str = "unknown") -> str:
    text = str(value or default).strip() or default
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", text)[:120]


def runtime_host_id(host_id: str | None = None) -> str:
    """Return a stable runtime host identifier for shared hook state scoping."""
    if host_id:
        return _safe_scope_part(host_id)
    for env_name in ("MEMENTO_HOST_ID", "MEMENTO_HOST", "MEMENTO_CLIENT", "MEMENTO_ADAPTER"):
        value = os.environ.get(env_name)
        if value:
            return _safe_scope_part(value)
    return "unknown-host"


def _runtime_scope(
    session_id: str = "unknown",
    cwd: str = "",
    project_slug: str | None = None,
    host_id: str | None = None,
) -> dict:
    if project_slug is None:
        if cwd:
            try:
                project_slug, _ = detect_project(cwd, None)
            except Exception:
                project_slug = "unknown"
        else:
            project_slug = "unknown"
    try:
        normalized_cwd = os.path.realpath(os.path.expanduser(cwd)).rstrip("/") if cwd else ""
    except (OSError, ValueError):
        normalized_cwd = str(Path(cwd).expanduser()) if cwd else ""
    scope = {
        "host_id": runtime_host_id(host_id),
        "project_slug": project_slug or "unknown",
        "session_id": session_id or "unknown",
        "cwd": normalized_cwd,
    }
    raw = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    scope["scope_id"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return scope


def _scope_storage_key(scope: dict) -> str:
    return ":".join(
        [
            _safe_scope_part(scope.get("host_id")),
            _safe_scope_part(scope.get("project_slug")),
            _safe_scope_part(scope.get("session_id")),
            _safe_scope_part(scope.get("scope_id")),
        ]
    )


def _deferred_scope(project_slug: str, session_id: str, cwd: str = "", host_id: str | None = None) -> dict:
    return _runtime_scope(session_id, cwd, project_slug, host_id)


def deferred_briefing_path(
    project_slug: str = "unknown", session_id: str = "unknown", cwd: str = "", host_id: str | None = None
) -> str:
    """Return the host/session/project-scoped deferred briefing file path."""
    scope = _deferred_scope(project_slug, session_id, cwd, host_id)
    base = Path(DEFERRED_BRIEFING_PATH)
    return str(base.with_name(f"{base.stem}-{scope['scope_id']}{base.suffix}"))


def _lock_file(file_obj) -> None:
    try:
        import fcntl

        fcntl.flock(file_obj, fcntl.LOCK_EX)
    except (ImportError, OSError):
        pass


def _read_json_locked(path: str, default=None):
    try:
        with open(path, "a+") as f:
            _lock_file(f)
            f.seek(0)
            raw = f.read()
            if not raw.strip():
                return default
            return json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json_locked(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as f:
        _lock_file(f)
        f.seek(0)
        f.truncate()
        json.dump(payload, f)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


def _deferred_is_expired(data: dict, ttl_seconds: int = 60) -> bool:
    timestamp = data.get("timestamp") or data.get("params", {}).get("timestamp")
    try:
        timestamp_float = float(timestamp)
    except (TypeError, ValueError):
        return True
    return (time.time() - timestamp_float) > ttl_seconds


def _deferred_scope_matches(data: dict, project_slug: str, session_id: str, host_id: str | None = None) -> bool:
    scope = data.get("scope") or data.get("params", {}).get("scope") or {}
    if not isinstance(scope, dict):
        return False
    expected = _deferred_scope(project_slug or "unknown", session_id or "unknown", scope.get("cwd", ""), host_id)
    return (
        scope.get("project_slug") == expected["project_slug"]
        and scope.get("session_id") == expected["session_id"]
        and scope.get("host_id", "unknown-host") == expected["host_id"]
    )


def cleanup_deferred_briefings(max_files: int = 100) -> None:
    """Remove expired/corrupt deferred files and cap scoped deferred state."""
    base = Path(DEFERRED_BRIEFING_PATH)
    parent = base.parent
    if not parent.exists():
        return
    candidates = [p for p in parent.glob(f"{base.stem}-*{base.suffix}") if p != base]
    live: list[tuple[float, Path]] = []
    for path in candidates:
        data = _read_json_locked(str(path), default=None)
        if not isinstance(data, dict) or _deferred_is_expired(data, DEFERRED_BRIEFING_TTL_SECONDS):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        timestamp = data.get("timestamp") or data.get("params", {}).get("timestamp") or 0
        try:
            live.append((float(timestamp), path))
        except (TypeError, ValueError):
            live.append((0.0, path))
    for _ts, path in sorted(live, reverse=True)[max_files:]:
        try:
            path.unlink()
        except OSError:
            pass


def _cleanup_legacy_deferred_briefing(scoped_path: str) -> None:
    """Remove unscoped legacy deferred results instead of allowing cross-session consumption."""
    legacy_path = Path(DEFERRED_BRIEFING_PATH)
    if str(legacy_path) == scoped_path or not legacy_path.exists():
        return
    try:
        with legacy_path.open() as f:
            data = json.load(f)
        if not data.get("scope") and not data.get("params", {}).get("scope"):
            legacy_path.unlink()
    except (json.JSONDecodeError, OSError):
        try:
            legacy_path.unlink()
        except OSError:
            pass


def spawn_deferred_search(
    project_slug, git_branch, linked_notes, config, session_id="unknown", host_id: str | None = None
):
    """Spawn a background subprocess to run QMD search and write scoped results."""
    max_notes = config.get("briefing_max_notes", 5)
    min_score = config.get("briefing_min_score", 0.3)

    # Build search query
    query_parts = [project_slug.replace("-", " ")]
    if git_branch and git_branch not in ("main", "master", "HEAD"):
        branch_words = git_branch.replace("-", " ").replace("/", " ")
        query_parts.append(branch_words)

    cwd = config.get("_cwd", "")
    scope = _deferred_scope(project_slug, session_id, cwd, host_id)
    deferred_path = deferred_briefing_path(project_slug, session_id, cwd, host_id)
    cleanup_deferred_briefings()

    # Write the search params for the background worker
    params = {
        "query": " ".join(query_parts),
        "max_notes": max_notes,
        "min_score": min_score,
        "linked_notes": linked_notes,
        "cwd": cwd,
        "timestamp": time.time(),
        "ttl_seconds": DEFERRED_BRIEFING_TTL_SECONDS,
        "scope": scope,
    }

    worker = _find_hook_script("vault-briefing.py")
    if worker is None:
        log_retrieval(
            "briefing",
            "deferred-worker-missing",
            script="vault-briefing.py",
            search_base=str(Path(__file__).resolve().parent.parent),
        )
        return

    try:
        _write_json_locked(
            deferred_path,
            {"status": "pending", "params": params, "scope": scope, "timestamp": params["timestamp"]},
        )

        # Spawn background worker — the same script with --deferred flag
        _subprocess.Popen(
            [sys.executable, str(worker), "--deferred", "--deferred-path", deferred_path],
            stdin=_subprocess.DEVNULL,
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        # If spawn fails, clean up so recall doesn't wait for stale pending
        try:
            os.unlink(deferred_path)
        except OSError:
            pass


def run_deferred_briefing_search(deferred_path: str | None = None):
    """Background worker: run QMD search and write results to the scoped deferred file."""
    path = deferred_path or DEFERRED_BRIEFING_PATH
    try:
        data = _read_json_locked(path, default={})

        if data.get("status") != "pending" or _deferred_is_expired(data, DEFERRED_BRIEFING_TTL_SECONDS):
            try:
                os.unlink(path)
            except OSError:
                pass
            sys.exit(0)

        params = data["params"]
        query = params["query"]
        max_notes = params["max_notes"]
        min_score = params["min_score"]
        linked_notes = params.get("linked_notes", [])
        scope = params.get("scope") or data.get("scope") or _deferred_scope("unknown", "unknown", params.get("cwd", ""))

        import time as _time

        t0 = _time.time()
        results = qmd_search(
            query,
            limit=max_notes + 3,
            semantic=True,
            timeout=12,
            min_score=min_score,
        )
        latency_ms = int((_time.time() - t0) * 1000)

        results = enhance_results(results, cwd=params.get("cwd", ""))

        # Format results, dedup against linked notes
        seen = set()
        note_lines = []
        note_paths = []

        for result in results:
            title = result.get("title", "")
            if title in seen:
                continue
            seen.add(title)
            note_lines.append(format_qmd_result(result))
            note_paths.append(result.get("path", ""))

        for note_name in linked_notes:
            if note_name in seen or len(note_lines) >= max_notes:
                break
            seen.add(note_name)
            oneliner = read_note_oneliner(note_name)
            if oneliner:
                note_lines.append(f"  - {oneliner}")
                note_paths.append(f"notes/{note_name}.md")

        final_notes = note_lines[:max_notes]
        final_paths = [path for path in note_paths[:max_notes] if path]
        if final_paths:
            record_access(
                final_paths,
                hook="briefing",
                tool="deferred-search",
                query=query,
                session_id=params.get("session_id", "unknown"),
                result_count=len(final_paths),
            )
        _write_json_locked(
            path,
            {
                "status": "ready",
                "note_lines": final_notes,
                "timestamp": time.time(),
                "ttl_seconds": DEFERRED_BRIEFING_TTL_SECONDS,
                "scope": scope,
            },
        )

        injected_chars = sum(len(line) for line in final_notes)
        log_retrieval(
            "briefing",
            "deferred-ready",
            query=query,
            latency_ms=latency_ms,
            injected_count=len(final_notes),
            injected_chars=injected_chars,
        )

    except Exception:
        # Clean up on failure
        try:
            os.unlink(path)
        except OSError:
            pass


def run_remote_briefing(cwd, config, session_id="unknown", host_id: str | None = None):
    """Run briefing via the remote vault client. Returns content or None."""
    from memento.remote_client import status as remote_status, search as remote_search

    vault_status = remote_status()
    if not vault_status or "error" in vault_status:
        return None

    note_count = vault_status.get("note_count", 0)
    git_branch = get_git_branch(cwd)
    project_slug, _ticket = detect_project(cwd, git_branch)
    if project_slug == "unknown":
        return None

    branch_str = f" ({git_branch})" if git_branch else ""
    summary = f"[vault] Project: {project_slug}{branch_str} | {note_count} notes (remote)"

    max_notes = config.get("briefing_max_notes", 5)
    query = project_slug.replace("-", " ")
    if git_branch and git_branch not in ("main", "master", "HEAD"):
        query += " " + git_branch.replace("-", " ").replace("/", " ")

    results = remote_search(query=query, limit=max_notes, cwd=cwd)
    if results:
        note_lines = []
        for result in results[:max_notes]:
            title = result.get("title", "")
            note_lines.append(f"  - {title}")

        scope = _deferred_scope(project_slug, session_id, cwd, host_id)
        deferred_path = deferred_briefing_path(project_slug, session_id, cwd, host_id)
        _write_json_locked(
            deferred_path,
            {
                "status": "ready",
                "note_lines": note_lines,
                "timestamp": time.time(),
                "ttl_seconds": DEFERRED_BRIEFING_TTL_SECONDS,
                "source": "remote",
                "scope": scope,
            },
        )

    return summary


def build_briefing(
    cwd: str, session_id: str = "unknown", *, allow_deferred: bool = True, host_id: str | None = None
) -> LifecycleResult:
    """Build session-start briefing content."""
    config = get_config()
    host_id = runtime_host_id(host_id)
    metadata = {"cwd": cwd, "session_id": session_id, "host_id": host_id}

    def no_briefing(reason: str) -> LifecycleResult:
        return LifecycleResult(False, "", "briefing", reason=reason, metadata=metadata)

    if not config.get("session_briefing", True):
        return no_briefing("disabled")
    if not cwd:
        return no_briefing("missing-cwd")

    from memento.remote_client import is_remote

    if is_remote() and allow_deferred:
        try:
            remote_content = run_remote_briefing(cwd, config, session_id, host_id=host_id)
            if remote_content:
                return LifecycleResult(True, remote_content, "briefing", metadata={**metadata, "remote": True})
        except Exception as exc:
            metadata["remote_error"] = str(exc)
            print(f"[memento] remote vault unreachable, using local only ({exc})", file=sys.stderr)

    vault = get_vault()
    if not vault.exists() or not (vault / "notes").exists():
        return no_briefing("vault-unavailable")

    git_branch = get_git_branch(cwd)
    project_slug, _ticket = detect_project(cwd, git_branch)
    metadata["project_slug"] = project_slug
    metadata["branch"] = git_branch
    if project_slug == "unknown":
        return no_briefing("unknown-project")

    recent_sessions, linked_notes = read_project_index(project_slug)
    notes_dir = vault / "notes"
    note_count = len(list(notes_dir.glob("*.md"))) if notes_dir.exists() else 0

    branch_str = f" ({git_branch})" if git_branch else ""
    last_date = ""
    if recent_sessions:
        last_line = recent_sessions[-1]
        date_match = re.match(r"(\d{4}-\d{2}-\d{2})", last_line)
        if date_match:
            last_date = f", last: {date_match.group(1)}"

    lines = [
        f"[vault] Project: {project_slug}{branch_str} | {len(recent_sessions)} sessions{last_date} | {note_count} notes"
    ]

    for warning in (
        triage_health_warning(rate_limited=True),
        pi_bridge_health_warning(rate_limited=True),
    ):
        if warning:
            lines.append(warning)

    if allow_deferred and config.get("project_maps_enabled", True) and has_qmd():
        try:
            max_notes = config.get("briefing_max_notes", 5)
            map_notes = lookup_project_notes(project_slug, limit=max_notes)
            if len(map_notes) >= max_notes:
                note_lines = []
                for note in map_notes[:max_notes]:
                    title = note.get("title", "")
                    note_lines.append(f"  - {title}")
                record_access_hits("briefing", "project-maps", map_notes[:max_notes], session_id=session_id)
                scope = _deferred_scope(project_slug, session_id, cwd, host_id)
                _write_json_locked(
                    deferred_briefing_path(project_slug, session_id, cwd, host_id),
                    {
                        "status": "ready",
                        "note_lines": note_lines,
                        "timestamp": time.time(),
                        "ttl_seconds": DEFERRED_BRIEFING_TTL_SECONDS,
                        "source": "project-maps",
                        "scope": scope,
                    },
                )
                log_retrieval(
                    "briefing", "project-maps-fast-path", project=project_slug, injected_count=len(note_lines)
                )
                return LifecycleResult(True, "\n".join(lines), "briefing", metadata=metadata)
        except Exception:
            pass

    try:
        load_or_build_graph(get_vault())
    except Exception:
        pass

    if allow_deferred and has_qmd():
        config["_cwd"] = cwd
        spawn_deferred_search(project_slug, git_branch, linked_notes, config, session_id=session_id, host_id=host_id)

    return LifecycleResult(True, "\n".join(lines), "briefing", metadata=metadata)


RECALL_CONTROL_WORDS = {
    "a",
    "after",
    "again",
    "ahead",
    "all",
    "and",
    "continue",
    "do",
    "for",
    "fresh",
    "go",
    "it",
    "lets",
    "next",
    "ok",
    "on",
    "one",
    "ship",
    "slice",
    "start",
    "the",
    "this",
    "to",
    "what",
    "whats",
    "is",
}

RECALL_DOMAIN_ALLOWLIST = {
    "backend",
    "capture",
    "dedup",
    "extract",
    "lifecycle",
    "mcp",
    "queue",
    "sync",
    "ticket",
    "tolgee",
}

LOW_SIGNAL_RECALL_PATTERNS = (
    r"^(ok[, ]*)?(go for it|go ahead|do it|continue|start fresh|lets start fresh|let's start fresh|ship it)$",
    r"^(go for the )?(next|next one|next slice)$",
    r"^what (is|'s|s) the next( slice| step| feature)?\??$",
    r"^go for the [a-z0-9 _-]+ cleanup$",
)

BROAD_PROJECT_HISTORY_PATTERNS = (
    r"^what previous decisions did we make (?:on|about|for) (?P<subject>[a-z0-9][a-z0-9 _-]*)\??$",
    r"^what do we know about (?P<subject>[a-z0-9][a-z0-9 _-]*)\??$",
    r"^summari[sz]e (?P<subject>[a-z0-9][a-z0-9 _-]*?)(?: history)?\??$",
    r"^what was decided before about (?P<subject>[a-z0-9][a-z0-9 _-]*)\??$",
)

PROJECT_ENTITY_STOPWORDS = {
    "what",
    "previous",
    "decisions",
    "make",
    "know",
    "about",
    "summarize",
    "summarise",
    "history",
    "decided",
    "before",
}

SPECIFIC_PROJECT_QUERY_PATTERNS = (r"\bwhat did we decide about (?P<subject>[a-z0-9][a-z0-9 _-]*)",)


def recall_signal_terms(prompt: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9-]+", prompt.lower())
    signal_terms = []
    for token in tokens:
        if token in RECALL_CONTROL_WORDS:
            continue
        if len(token) <= 2:
            continue
        signal_terms.append(token)
    return signal_terms


def is_low_signal_recall_prompt(prompt: str) -> bool:
    """Return True for turn-control prompts that should not search memory."""
    normalized = re.sub(r"\s+", " ", prompt.lower()).strip().strip(".!?")
    if not normalized:
        return True
    for pattern in LOW_SIGNAL_RECALL_PATTERNS:
        if re.match(pattern, normalized):
            return True

    signal_terms = recall_signal_terms(normalized)
    if len(signal_terms) >= 2:
        return False
    if len(signal_terms) == 1 and signal_terms[0] in RECALL_DOMAIN_ALLOWLIST:
        return False
    return True


def _subject_is_broad(subject: str) -> bool:
    terms = recall_signal_terms(subject)
    # Broad project/history questions usually name only a project or a short
    # project phrase. Longer subjects carry domain intent and should use recall.
    return 0 < len(terms) <= 3


def is_broad_project_history_query(prompt: str) -> bool:
    """Return True when a prompt asks for broad project history, not turn context."""
    normalized = re.sub(r"\s+", " ", prompt.lower()).strip().strip(".!")
    if not normalized:
        return False
    for pattern in BROAD_PROJECT_HISTORY_PATTERNS:
        match = re.match(pattern, normalized)
        if match and _subject_is_broad(match.group("subject")):
            return True
    return False


def should_append_project_to_recall(prompt: str, concrete: bool = False) -> bool:
    """Only append project slug when the prompt has enough standalone signal."""
    return not concrete and not is_low_signal_recall_prompt(prompt) and not is_broad_project_history_query(prompt)


def resolve_recall_concrete_mode(prompt: str, config: dict) -> tuple[bool, bool]:
    """Resolve the configured prompt-recall concrete mode against the prompt."""
    return resolve_concrete_mode(config.get("recall_concrete_mode", False), prompt)


def _project_slug_from_value(value: str | None) -> str:
    if not value:
        return ""
    value = str(value).strip().strip('"').strip("'")
    if not value:
        return ""
    if "/" in value or "\\" in value:
        value = Path(value).name
    return slugify(value)


def _candidate_project_slug(result: dict) -> str:
    project = result.get("project")
    meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else None
    if not project and meta:
        project = meta.get("project")
    if not project:
        note_path = result.get("path", "")
        if note_path:
            meta = read_note_metadata(note_path)
            if meta:
                project = meta.get("project")
    return _project_slug_from_value(project)


def _subject_project_slug(subject: str) -> str:
    terms = recall_signal_terms(subject)
    if not terms:
        return ""
    slug = slugify(terms[0])
    if not slug or slug in PROJECT_ENTITY_STOPWORDS or slug in RECALL_DOMAIN_ALLOWLIST:
        return ""
    return slug


def _prompt_project_slugs(prompt: str) -> set[str]:
    slugs = set()
    for pattern in SPECIFIC_PROJECT_QUERY_PATTERNS:
        match = re.search(pattern, prompt.lower())
        if not match:
            continue
        slug = _subject_project_slug(match.group("subject"))
        if slug:
            slugs.add(slug)
    return slugs


def _explicit_project_slugs(prompt: str, results: list[dict]) -> set[str]:
    normalized_prompt = f" {slugify(prompt).replace('-', ' ')} "
    slugs = set()

    for result in results:
        candidate_slug = _candidate_project_slug(result)
        if not candidate_slug:
            continue
        phrase = candidate_slug.replace("-", " ")
        if f" {phrase} " in normalized_prompt:
            slugs.add(candidate_slug)

    slugs.update(_prompt_project_slugs(prompt))

    return slugs


def filter_recall_results_by_explicit_project(prompt: str, results: list[dict]) -> tuple[list[dict], list[dict]]:
    """Drop results with explicit mismatching project metadata.

    This is intentionally conservative: it only acts when the prompt names a
    project and a candidate declares a different project. Unscoped notes remain
    eligible as general knowledge.
    """
    explicit_slugs = _explicit_project_slugs(prompt, results)
    if not explicit_slugs:
        return results, []

    filtered = []
    decisions = []
    for result in results:
        candidate_slug = _candidate_project_slug(result)
        if not candidate_slug:
            filtered.append(result)
            decisions.append(_candidate_summary(result, "no-project-metadata"))
        elif candidate_slug in explicit_slugs:
            filtered.append(result)
            decisions.append(_candidate_summary(result, "project-match"))
        else:
            decisions.append(_candidate_summary(result, "project-mismatch"))
    return filtered, decisions


def recall_diagnostics_enabled(config: dict) -> bool:
    return bool(config.get("recall_diagnostics", False))


def _candidate_summary(result: dict, decision: str = "candidate") -> dict:
    return {
        "path": result.get("path", ""),
        "title": _strip_injection(result.get("title", "")),
        "score": round(float(result.get("score", 0) or 0), 4),
        "decision": decision,
    }


def log_recall_diagnostic(config: dict, action: str, **kwargs) -> None:
    """Log opt-in prompt recall diagnostics without changing recall behavior."""
    if not recall_diagnostics_enabled(config):
        return
    log_retrieval("recall", f"diagnostic-{action}", **kwargs)


def log_recall_candidates(config: dict, results: list[dict], stage: str, **kwargs) -> None:
    if not recall_diagnostics_enabled(config) or not config.get("recall_diagnostics_include_candidates", False):
        return
    max_candidates = int(config.get("recall_diagnostics_max_candidates", 10) or 10)
    candidates = [_candidate_summary(result) for result in results[: max(0, max_candidates)]]
    log_recall_diagnostic(config, "candidates", stage=stage, candidates=candidates, **kwargs)


def should_skip_recall(prompt, config, concrete: bool = False):
    """Relevance gate — returns True if we should skip vault injection."""
    prompt = prompt.strip()

    # Concrete recall is opt-in and intentionally bypasses the length/
    # low-signal/broad-project gates so exact paths, env vars, UUIDs, and
    # quoted phrases can go straight to the literal search path.
    if not concrete:
        # Too short
        if len(prompt) < 10:
            return True

        if config.get("recall_skip_low_signal", True) and is_low_signal_recall_prompt(prompt):
            return True

        if config.get("recall_skip_broad_project_queries", True) and is_broad_project_history_query(prompt):
            return True

    # Skill invocation
    if prompt.startswith("/"):
        return True

    # Skill expansions and command messages (XML tags from hook system)
    if "<command-message>" in prompt or "<command-name>" in prompt:
        return True
    if "<task-notification>" in prompt:
        return True

    # Skill content dumps (headers from expanded skills)
    if prompt.startswith("# ") and len(prompt) > 200:
        return True

    # Ticket context injections from start-ticket and similar skills
    if "You are working on" in prompt:
        return True
    if prompt.startswith("Continuation guidance:"):
        return True

    # Local command caveats
    if "<local-command-caveat>" in prompt:
        return True

    # Long prompts are almost always skill expansions, not user input
    # Real user prompts rarely exceed 200 chars
    if len(prompt) > 200:
        return True

    # Match skip patterns
    skip_patterns = config.get("recall_skip_patterns", [])
    prompt_lower = prompt.lower().strip()
    for pattern in skip_patterns:
        try:
            if re.match(pattern, prompt_lower, re.IGNORECASE):
                return True
        except re.error:
            continue

    return False


RECALL_DEDUP_MAX_SESSIONS = 50
RECALL_DEDUP_SESSION_TTL_HOURS = 48


def _recall_dedup_prompts():
    try:
        return int(get_config().get("recall_dedup_prompts", 3))
    except (TypeError, ValueError):
        return 3


def _recall_scope_key(
    session_id: str = "unknown",
    cwd: str = "",
    project_slug: str | None = None,
    host_id: str | None = None,
) -> tuple[str, dict]:
    scope = _runtime_scope(session_id, cwd, project_slug, host_id)
    return _scope_storage_key(scope), scope


def _prune_recall_dedup(state):
    """Bound dedup state: drop idle scoped sessions, cap total scope count."""
    sessions = state.get("sessions", {})
    cutoff = time.time() - RECALL_DEDUP_SESSION_TTL_HOURS * 3600
    sessions = {sid: s for sid, s in sessions.items() if isinstance(s, dict) and s.get("updated", 0) >= cutoff}
    if len(sessions) > RECALL_DEDUP_MAX_SESSIONS:
        newest = sorted(sessions.items(), key=lambda kv: kv[1].get("updated", 0), reverse=True)
        sessions = dict(newest[:RECALL_DEDUP_MAX_SESSIONS])
    state["sessions"] = sessions
    state["schema"] = 2
    return state


def _mutate_recall_dedup(mutator):
    """Read-modify-write the dedup state under an exclusive file lock.

    The state file is shared by Claude hooks, the Pi bridge, and MCP; without
    the lock one host's write could clobber another's. Fail-open: dedup is an
    optimization, never worth breaking recall over.
    """
    try:
        os.makedirs(os.path.dirname(RECALL_DEDUP_PATH), exist_ok=True)
        with open(RECALL_DEDUP_PATH, "a+") as f:
            _lock_file(f)
            f.seek(0)
            raw = f.read()
            try:
                state = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                state = {}
            if not isinstance(state, dict):
                state = {}
            state = _prune_recall_dedup(state)
            result = mutator(state)
            state = _prune_recall_dedup(state)
            f.seek(0)
            f.truncate()
            json.dump(state, f)
            return result
    except Exception:
        return None


def recently_injected_paths(
    session_id: str = "unknown",
    cwd: str = "",
    project_slug: str | None = None,
    host_id: str | None = None,
):
    """Paths recall injected into this host/session/project scope within the dedup window."""
    key, _scope = _recall_scope_key(session_id, cwd, project_slug, host_id)

    def read(state):
        entry = state.get("sessions", {}).get(key, {})
        return set((entry.get("injected") or {}).keys())

    return _mutate_recall_dedup(read) or set()


def record_recall(
    paths,
    session_id: str = "unknown",
    cwd: str = "",
    project_slug: str | None = None,
    host_id: str | None = None,
):
    """Remember every injected path for this host/session/project scope for N prompts."""
    prompts = _recall_dedup_prompts()
    key, scope = _recall_scope_key(session_id, cwd, project_slug, host_id)

    def write(state):
        entry = state.setdefault("sessions", {}).setdefault(key, {"scope": scope})
        entry["scope"] = scope
        injected = entry.setdefault("injected", {})
        for path in paths if isinstance(paths, (list, tuple, set)) else [paths]:
            if path:
                injected[str(path)] = prompts
        entry["updated"] = time.time()

    _mutate_recall_dedup(write)


def record_access_hits(
    hook: str, tool: str, results: list[dict], *, query: str | None = None, session_id: str = "unknown"
):
    """Write derived access-log entries for successful retrieval hits."""
    paths = [str(result.get("path", "")).strip() for result in results if str(result.get("path", "")).strip()]
    if not paths:
        return
    record_access(paths, hook=hook, tool=tool, query=query, session_id=session_id, result_count=len(paths))


def bump_prompts_since(
    session_id: str = "unknown",
    cwd: str = "",
    project_slug: str | None = None,
    host_id: str | None = None,
):
    """Age this scoped session's dedup entries by one prompt; drop expired paths."""
    key, _scope = _recall_scope_key(session_id, cwd, project_slug, host_id)

    def write(state):
        entry = state.get("sessions", {}).get(key)
        if not entry:
            return
        injected = entry.get("injected") or {}
        for path in list(injected):
            try:
                injected[path] = int(injected[path]) - 1
            except (TypeError, ValueError):
                injected[path] = 0
            if injected[path] <= 0:
                del injected[path]
        entry["updated"] = time.time()

    _mutate_recall_dedup(write)


def _strip_injection(text):
    """Strip instruction-like patterns from injected content (defense-in-depth)."""
    if not text:
        return text
    # Remove patterns that could be interpreted as system instructions
    text = re.sub(r"(?i)(ignore\s+(all\s+)?previous\s+instructions)", "[filtered]", text)
    text = re.sub(r"(?i)(you\s+are\s+now\s+|you\s+must\s+now\s+)", "[filtered]", text)
    text = re.sub(r"(?i)^(system|assistant)\s*:", "[filtered]:", text)
    text = re.sub(r"</?s>", "", text)
    return text


def format_result(result):
    """Format a QMD result as a compact one-liner."""
    title = _strip_injection(result.get("title", ""))
    snippet = _strip_injection(result.get("snippet", "").strip())

    # Truncate snippet to first sentence or 120 chars
    if snippet:
        dot = snippet.find(".")
        if 0 < dot < 120:
            snippet = snippet[: dot + 1]
        elif len(snippet) > 120:
            snippet = snippet[:120] + "..."

    line = f"  - {title}"
    if snippet:
        line += f": {snippet}"
    return line


def consume_deferred_briefing(
    cwd: str = "", session_id: str = "unknown", project_slug: str | None = None, host_id: str | None = None
):
    """Check for this host/session/project's deferred briefing and consume it.

    Returns formatted lines to prepend, or empty list. Results are scoped by
    host, project, and session and expire after ``DEFERRED_BRIEFING_TTL_SECONDS``.
    Pending files are left intact until they expire; ready files are deleted on
    successful consumption or rejected when stale/corrupt.
    """
    if project_slug is None:
        git_branch = get_git_branch(cwd) if cwd else ""
        project_slug, _ticket = detect_project(cwd, git_branch) if cwd else ("unknown", None)

    host_id = runtime_host_id(host_id)
    cleanup_deferred_briefings()
    path = deferred_briefing_path(project_slug or "unknown", session_id or "unknown", cwd, host_id)
    _cleanup_legacy_deferred_briefing(path)

    try:
        if not os.path.exists(path):
            return []

        data = _read_json_locked(path, default={})

        status = data.get("status", "")

        if _deferred_is_expired(data, DEFERRED_BRIEFING_TTL_SECONDS):
            os.unlink(path)
            return []

        if not _deferred_scope_matches(data, project_slug or "unknown", session_id or "unknown", host_id):
            os.unlink(path)
            return []

        if status == "pending":
            # Background worker still running; this session can pick it up on a later prompt.
            return []

        if status != "ready":
            os.unlink(path)
            return []

        # Got results — consume and clean up
        note_lines = data.get("note_lines", [])
        os.unlink(path)

        # Mark vsearch as warm for RRF hybrid search
        try:
            mark_vsearch_warm()
        except Exception:
            pass

        if not note_lines:
            return []

        return ["[vault] Relevant notes:"] + note_lines

    except (json.JSONDecodeError, OSError, KeyError):
        try:
            os.unlink(path)
        except OSError:
            pass
        return []


def spawn_deep_recall(prompt, initial_results, config):
    """Spawn a background codex process for deeper vault analysis.

    Writes the prompt + initial results to a temp input file, then spawns
    a detached process that calls codex and writes structured output to
    DEEP_RECALL_PENDING_PATH.

    Only called when the prompt is complex (multi-hop gate + low confidence).
    """
    backend = config.get("deep_recall_backend", "codex")

    worker = _find_hook_script("vault-recall.py")
    if worker is None:
        log_retrieval(
            "recall",
            "deep-recall-worker-missing",
            script="vault-recall.py",
            search_base=str(Path(__file__).resolve().parent.parent),
        )
        return

    # Build context from initial results
    context_lines = []
    for r in initial_results[:5]:
        title = r.get("title", "")
        snippet = r.get("snippet", "").strip()
        path = r.get("path", "")
        context_lines.append(f"- {title} ({path}): {snippet[:200]}")

    input_data = {
        "prompt": prompt,
        "initial_results": context_lines,
        "timestamp": time.time(),
    }

    try:
        # Write input for the background worker
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        input_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="deep-recall-input-",
            dir=RUNTIME_DIR,
            delete=False,
        )
        json.dump(input_data, input_file)
        input_file.close()

        # Mark as pending
        with open(DEEP_RECALL_PENDING_PATH, "w") as f:
            json.dump({"status": "pending", "timestamp": time.time()}, f)

        # Spawn background worker
        _subprocess.Popen(
            [
                sys.executable,
                str(worker),
                "--deep-recall",
                input_file.name,
                backend,
            ],
            stdin=_subprocess.DEVNULL,
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        # Clean up on spawn failure
        try:
            os.unlink(DEEP_RECALL_PENDING_PATH)
        except OSError:
            pass
        try:
            os.unlink(input_file.name)
        except (OSError, UnboundLocalError):
            pass


def run_deep_recall_worker(input_path, backend):
    """Background worker: run codex analysis and write results.

    Called as a detached subprocess via --deep-recall flag.
    """
    try:
        with open(input_path) as f:
            input_data = json.load(f)
    except (OSError, json.JSONDecodeError):
        _cleanup_deep_recall_pending()
        return
    finally:
        try:
            os.unlink(input_path)
        except OSError:
            pass

    prompt = input_data.get("prompt", "")
    context_lines = input_data.get("initial_results", [])

    if not prompt:
        _cleanup_deep_recall_pending()
        return

    context_block = "\n".join(context_lines) if context_lines else "(no initial results)"

    codex_prompt = (
        "You are a vault recall assistant. The user asked:\n\n"
        f"{prompt}\n\n"
        "Initial search found these vault notes:\n"
        f"{context_block}\n\n"
        "Based on the user's question and the notes above, identify what "
        "additional vault notes would be relevant. Think about:\n"
        "- Related decisions or patterns from other projects\n"
        "- Temporal context (what changed, previous approaches)\n"
        "- Cross-references between the found notes\n\n"
        "Return a JSON array of objects with 'title' and 'reason' keys. "
        "Each title should be the likely title of a vault note that would help. "
        "Return at most 3 suggestions. If nothing additional is needed, return []."
    )

    try:
        result = llm_complete(
            codex_prompt,
            {
                "llm_backend": backend,
            },
        )
        raw = result.text if result.ok else ""

        # Parse the LLM response — extract JSON array
        suggestions = _parse_deep_recall_response(raw)

        with open(DEEP_RECALL_PENDING_PATH, "w") as f:
            json.dump(
                {
                    "status": "ready",
                    "suggestions": suggestions,
                    "prompt": prompt,
                    "timestamp": time.time(),
                },
                f,
            )

    except OSError:
        _cleanup_deep_recall_pending()


def _parse_deep_recall_response(raw):
    """Extract a JSON array of suggestions from the LLM response."""
    if not raw:
        return []

    # Try direct JSON parse first
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [s for s in parsed if isinstance(s, dict) and "title" in s][:3]
    except json.JSONDecodeError:
        pass

    # Try extracting JSON from markdown code blocks
    match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, list):
                return [s for s in parsed if isinstance(s, dict) and "title" in s][:3]
        except json.JSONDecodeError:
            pass

    # Try finding a bare JSON array in the text
    match = re.search(r"\[.*?\]", raw, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                return [s for s in parsed if isinstance(s, dict) and "title" in s][:3]
        except json.JSONDecodeError:
            pass

    return []


def _cleanup_deep_recall_pending():
    """Remove the pending file on worker failure."""
    try:
        os.unlink(DEEP_RECALL_PENDING_PATH)
    except OSError:
        pass


def consume_deep_recall():
    """Check for pending deep recall results and consume them.

    Returns formatted lines to inject, or empty list.
    Same pattern as consume_deferred_briefing: pending = wait,
    ready = consume, stale (>60s) = discard.
    """
    try:
        if not os.path.exists(DEEP_RECALL_PENDING_PATH):
            return []

        with open(DEEP_RECALL_PENDING_PATH) as f:
            data = json.load(f)

        status = data.get("status", "")

        if status == "pending":
            ts = data.get("timestamp", 0)
            if ts and (time.time() - ts) > 60:
                os.unlink(DEEP_RECALL_PENDING_PATH)
            return []

        if status != "ready":
            os.unlink(DEEP_RECALL_PENDING_PATH)
            return []

        suggestions = data.get("suggestions", [])
        os.unlink(DEEP_RECALL_PENDING_PATH)

        if not suggestions:
            return []

        lines = ["[vault] Deep analysis suggests also reviewing:"]
        for s in suggestions[:3]:
            title = _strip_injection(s.get("title", ""))
            reason = _strip_injection(s.get("reason", ""))
            if title:
                line = f"  - {title}"
                if reason:
                    line += f": {reason}"
                lines.append(line)

        return lines if len(lines) > 1 else []

    except (json.JSONDecodeError, OSError, KeyError):
        try:
            os.unlink(DEEP_RECALL_PENDING_PATH)
        except OSError:
            pass
        return []


def run_remote_recall(prompt, cwd, config, session_id="unknown", concrete: bool = False, host_id: str | None = None):
    """Run recall via the remote vault client.

    Returns (lines, top_path, results, reason, project_decisions). A non-terminal
    remote miss may carry a structured reason; callers may still fall back to
    local search. An explicit project mismatch is terminal because local fallback
    would risk injecting the unrelated context the filter just removed.
    """
    from memento.remote_client import search_envelope as remote_search_envelope

    if should_skip_recall(prompt, config, concrete=concrete):
        reason = "broad-project-query" if is_broad_project_history_query(prompt) else "skipped-prompt"
        return [], None, [], reason, []

    max_notes = config.get("recall_max_notes", 3)
    min_score = config.get("recall_min_score", 0.4)

    envelope = remote_search_envelope(
        query=prompt, limit=max_notes + 3, min_score=min_score, cwd=cwd, concrete=concrete
    )
    raw_results = envelope.get("results", [])
    if concrete:
        results = raw_results
        project_decisions = []
    else:
        results, project_decisions = filter_recall_results_by_explicit_project(prompt, raw_results)
    if not results:
        if project_decisions:
            reason = "project-mismatch-filtered-empty"
        elif isinstance(envelope.get("miss"), dict):
            reason = envelope["miss"].get("reason") or "no-results"
            reason = StructuredMissReason(reason, envelope["miss"])
        else:
            reason = "no-results"
        return [], None, [], reason, project_decisions

    recent = recently_injected_paths(session_id, cwd=cwd, host_id=host_id)
    if recent:
        fresh = [r for r in results if r.get("path", "") not in recent]
        if not fresh:
            return [], None, [], "duplicate", project_decisions
        results = fresh
    top_path = results[0].get("path", "")

    lines = ["[vault] Related memories:"]
    injected_results = results[:max_notes]
    for result in injected_results:
        lines.append(format_result(result))

    return lines, top_path, injected_results, None, project_decisions


def _run_recall_lines_pipeline(prompt: str, cwd: str = "", session_id: str = "unknown", host_id: str | None = None):
    """Backward-compatible alias for the retrieval policy runtime."""
    return _run_recall_lines(prompt, cwd=cwd, session_id=session_id, host_id=host_id)


def _run_recall_lines(prompt: str, cwd: str = "", session_id: str = "unknown", host_id: str | None = None):
    """Run prompt recall through the retrieval policy runtime seam."""
    from memento.remote_client import is_remote, search_envelope as remote_search_envelope

    runtime = PromptRecallRuntime(
        config_loader=get_config,
        vault_loader=get_vault,
        has_backend=has_qmd,
        remote_available=is_remote,
        remote_search_envelope=remote_search_envelope,
        detect_project=lambda current_cwd: detect_project(current_cwd, None),
        qmd_search=qmd_search_with_extras,
        enhance_results=enhance_results,
        recently_injected_paths=recently_injected_paths,
        bump_prompts_since=bump_prompts_since,
        concept_lookup=lookup_concepts,
        prf_expand=prf_expand_query,
        semantic_warm=is_vsearch_warm,
        fuse_results=rrf_fuse,
        multi_hop=multi_hop_search,
        deep_recall_pending_exists=lambda: os.path.exists(DEEP_RECALL_PENDING_PATH),
        spawn_deep_recall=spawn_deep_recall,
        log_retrieval=log_retrieval,
    )
    decision = runtime.run(PromptRecallRequest(prompt=prompt, cwd=cwd, session_id=session_id, host_id=host_id))
    return decision.lines, decision.top_path, decision.results, decision.reason


def run_recall():
    """Backward-compatible hook helper. Returns (lines, top_path)."""
    try:
        hook_input = read_hook_input()
    except Exception as exc:
        log_retrieval("recall", "hook_input_failed", error=str(exc))
        return [], None

    lines, top_path, _results, _reason = _run_recall_lines(
        hook_input.get("prompt", ""),
        hook_input.get("cwd", ""),
        hook_input.get("session_id", "unknown"),
    )
    return lines, top_path


def build_recall(
    prompt: str,
    cwd: str = "",
    session_id: str = "unknown",
    *,
    record: bool = True,
    host_id: str | None = None,
) -> LifecycleResult:
    """Build prompt recall content."""
    host_id = runtime_host_id(host_id)
    lines, top_path, results, reason = _run_recall_lines(prompt, cwd, session_id, host_id=host_id)
    if not lines:
        if reason in ("broad-project-query", "skipped-prompt", "low-signal-prompt"):
            return empty_result("recall", reason)
        normalized = normalize_miss_reason(reason, prompt)
        if normalized in MISS_RECOVERY_HINTS:
            remote_miss = getattr(reason, "miss", None)
            details = None
            if normalized == "threshold_too_high" and not isinstance(remote_miss, dict):
                details = {"min_score": get_config().get("recall_min_score", 0.4)}
            result = empty_result("recall", normalized)
            result.metadata = {
                "miss": remote_miss if isinstance(remote_miss, dict) else build_search_miss(normalized, details=details)
            }
            return result
        return empty_result("recall", reason or "no-results")
    content = "\n".join(lines)
    if top_path and record:
        record_recall([r.get("path", "") for r in results], session_id, cwd=cwd, host_id=host_id)
        record_access_hits("recall", "inject", results, query=prompt, session_id=session_id)
    return LifecycleResult(
        should_inject=True,
        content=content,
        source="recall",
        results=results,
        metadata={"cwd": cwd, "session_id": session_id, "top_path": top_path},
    )


def _session_context_char_budget(token_budget: int | None) -> tuple[int, int]:
    """Return normalized token and character budgets for session context."""
    try:
        normalized_tokens = int(token_budget) if token_budget is not None else 2000
    except (TypeError, ValueError):
        normalized_tokens = 2000
    normalized_tokens = max(1, normalized_tokens)
    return normalized_tokens, normalized_tokens * 4


def _pi_state_root() -> Path:
    raw = os.environ.get("MEMENTO_PI_STATE_HOME")
    if raw:
        return Path(raw).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "memento" / "pi"


def _pi_queue_path_source() -> str:
    if os.environ.get("MEMENTO_PI_STATE_HOME"):
        return "memento_pi_state_home"
    if os.environ.get("XDG_STATE_HOME"):
        return "xdg_state_home"
    return "default_xdg_state"


def _pi_queue_file() -> Path:
    return _pi_state_root() / "queue" / "pi-captures.jsonl"


def _legacy_pi_queue_file(vault: Path) -> Path:
    return vault / "queue" / "pi-captures.jsonl"


def _queue_capture_keys(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []

    keys = []
    for line in lines:
        if not line.strip():
            continue
        try:
            capture = json.loads(line)
        except json.JSONDecodeError:
            keys.append(f"id:invalid-{len(keys) + 1}")
            continue
        if isinstance(capture, dict) and capture.get("id"):
            keys.append(f"id:{capture['id']}")
        else:
            keys.append(f"no-id:{path}:{len(keys) + 1}:{line}")
    return keys


def _queue_capture_count(path: Path) -> int:
    return len(_queue_capture_keys(path))


def _queue_capture_status(vault: Path) -> dict[str, object]:
    queue_path = _pi_queue_file()
    legacy_queue_path = _legacy_pi_queue_file(vault)
    current_capture_keys = _queue_capture_keys(queue_path)
    legacy_capture_keys = [] if queue_path == legacy_queue_path else _queue_capture_keys(legacy_queue_path)
    current_queued_capture_count = len(current_capture_keys)
    legacy_queued_capture_count = len(legacy_capture_keys)
    combined_queued_capture_count = len(dict.fromkeys(current_capture_keys + legacy_capture_keys))
    legacy_queue_exists = False if queue_path == legacy_queue_path else legacy_queue_path.exists()

    status: dict[str, object] = {
        "queued_capture_count": combined_queued_capture_count,
        "count": combined_queued_capture_count,
        "queued_capture_count_source": "current",
        "current_queued_capture_count": current_queued_capture_count,
        "queue_path": str(queue_path),
        "queue_path_source": _pi_queue_path_source(),
        "legacy_queue_path": str(legacy_queue_path),
        "legacy_queue_exists": legacy_queue_exists,
        "legacy_queued_capture_count": legacy_queued_capture_count,
    }
    if current_queued_capture_count == 0 and legacy_queued_capture_count:
        status["queued_capture_count_source"] = "legacy_fallback"
        status["queue_status_note"] = "using legacy vault queue fallback; pi_bridge migrates this queue to XDG state"
    elif legacy_queued_capture_count:
        status["queued_capture_count_source"] = "current_plus_legacy"
        status["queue_status_note"] = "including legacy vault queue captures pending pi_bridge migration"
    return status


def _compact_session_result(result: dict) -> dict:
    """Return bounded result metadata for session-context structured payloads."""
    compact = {
        "path": result.get("path", ""),
        "title": strip_injection(str(result.get("title", ""))),
    }
    if result.get("score") is not None:
        compact["score"] = result.get("score")
    snippet = strip_injection(str(result.get("snippet", "")).strip())
    if snippet:
        compact["snippet"] = snippet[:160] + ("..." if len(snippet) > 160 else "")
    return compact


def _compact_lifecycle_section(result: LifecycleResult) -> dict:
    """Return lifecycle result metadata without duplicating unbounded content."""
    section = {
        "should_inject": result.should_inject,
        "source": result.source,
        "result_count": len(result.results or []),
    }
    if result.reason is not None:
        section["reason"] = result.reason
    top_path = result.metadata.get("top_path") if isinstance(result.metadata, dict) else None
    if top_path:
        section["top_path"] = top_path
    miss = result.metadata.get("miss") if isinstance(result.metadata, dict) else None
    if isinstance(miss, dict):
        section["miss"] = {"reason": miss.get("reason"), "recovery_hints": miss.get("recovery_hints", [])[:2]}
    return section


def _unique_result_paths(results: list[dict]) -> list[str]:
    paths = []
    seen = set()
    for result in results:
        path = result.get("path") if isinstance(result, dict) else None
        if path and path not in seen:
            paths.append(path)
            seen.add(path)
    return paths


def _truncate_session_context(content: str, char_budget: int) -> tuple[str, bool]:
    if len(content) <= char_budget:
        return content, False
    suffix = "\n[vault] truncated; see expandable_paths for full notes"
    if char_budget <= len(suffix):
        return content[:char_budget], True
    return content[: char_budget - len(suffix)].rstrip() + suffix, True


def _finalize_session_context_payload(payload: dict) -> dict:
    payload["metadata"]["used_chars"] = len(payload.get("content", ""))
    payload["should_inject"] = bool(payload.get("content"))
    return payload


def _fit_session_context_payload(payload: dict, packet_char_budget: int) -> dict:
    """Keep the serialized session-context packet within budget plus metadata overhead."""
    if len(json.dumps(payload)) <= packet_char_budget:
        return _finalize_session_context_payload(payload)

    payload["metadata"]["truncated"] = True
    notes = payload["metadata"].setdefault("budget_notes", [])
    if "structured payload compacted to fit packet budget" not in notes:
        notes.append("structured payload compacted to fit packet budget")

    status_section = payload.get("sections", {}).get("status")
    if isinstance(status_section, dict) and isinstance(status_section.get("automation_memory"), dict):
        automation_memory = status_section["automation_memory"]
        status_section["automation_memory"] = {
            "ready": automation_memory.get("ready"),
            "status": automation_memory.get("status"),
            "readiness": automation_memory.get("readiness"),
            "probe": {"name": (automation_memory.get("probe") or {}).get("name")},
        }

    content = payload.get("content", "")
    if content:
        overage = len(json.dumps(payload)) - packet_char_budget
        new_len = max(0, len(content) - overage - 80)
        payload["content"] = content[:new_len].rstrip()

    if len(json.dumps(payload)) <= packet_char_budget:
        return _finalize_session_context_payload(payload)

    for result in payload.get("results", []):
        if "snippet" in result and len(result["snippet"]) > 40:
            result["snippet"] = result["snippet"][:40] + "..."

    if len(json.dumps(payload)) <= packet_char_budget:
        return _finalize_session_context_payload(payload)

    if len(json.dumps(payload)) > packet_char_budget:
        payload["metadata"].pop("cwd", None)
        payload["metadata"].pop("session_id", None)
        payload["metadata"].pop("warnings", None)
        payload["metadata"]["omitted_metadata"] = True

    if len(json.dumps(payload)) > packet_char_budget and "queue" in payload.get("sections", {}):
        payload["sections"]["queue"].pop("queue_path", None)

    expandable_paths = payload["metadata"].get("expandable_paths", [])
    while expandable_paths and len(json.dumps(payload)) > packet_char_budget:
        expandable_paths.pop()
        payload["metadata"]["omitted_expandable_paths_count"] = (
            payload["metadata"].get("omitted_expandable_paths_count", 0) + 1
        )

    if len(json.dumps(payload)) > packet_char_budget:
        payload["content"] = ""

    if len(json.dumps(payload)) > packet_char_budget:
        payload["sections"] = {
            key: value for key, value in payload.get("sections", {}).items() if key in {"status", "queue"}
        }
        payload["results"] = []
        payload["metadata"]["expandable_paths"] = []
        payload["metadata"]["omitted_results"] = True

    return _finalize_session_context_payload(payload)


def build_session_context(
    cwd: str = "",
    prompt: str = "",
    session_id: str = "unknown",
    token_budget: int | None = None,
    include_status: bool = True,
    include_recent: bool = True,
    include_recall: bool = True,
    include_tool_context_preview: bool = False,
    host_id: str | None = None,
) -> dict:
    """Build a one-call, budgeted session initialization/context packet."""
    host_id = runtime_host_id(host_id)
    token_budget, char_budget = _session_context_char_budget(token_budget)
    packet_char_budget = char_budget + 1200
    sections: dict[str, object] = {}
    raw_results: list[dict] = []
    content_blocks: list[str] = []
    warnings: list[str] = []

    if include_recent:
        briefing = build_briefing(cwd, session_id, allow_deferred=False, host_id=host_id)
        sections["briefing"] = _compact_lifecycle_section(briefing)
        if briefing.content:
            content_blocks.append(briefing.content)
        raw_results.extend(briefing.results or [])

    recall_top_path = None
    recall_content_marker = None
    if include_recall and prompt:
        recall = build_recall(prompt, cwd, session_id, record=False, host_id=host_id)
        sections["recall"] = _compact_lifecycle_section(recall)
        recall_top_path = recall.metadata.get("top_path") if isinstance(recall.metadata, dict) else None
        if recall.content:
            recall_content_marker = recall.content.splitlines()[0]
            content_blocks.append(recall.content)
        raw_results.extend(recall.results or [])

    if include_status:
        vault = get_vault()
        notes_dir = vault / "notes"
        projects_dir = vault / "projects"
        for warning in (
            triage_health_warning(rate_limited=True),
            pi_bridge_health_warning(rate_limited=True),
        ):
            if warning:
                warnings.append(warning)
        status = {
            "vault_exists": vault.exists(),
            "qmd_available": has_qmd(),
            "note_count": len(list(notes_dir.glob("*.md"))) if notes_dir.exists() else 0,
            "project_count": len(list(projects_dir.glob("*.md"))) if projects_dir.exists() else 0,
        }
        automation_memory = build_automation_memory_readiness(
            config=get_config(),
            vault=vault,
            qmd_available=status["qmd_available"],
        )
        automation_metadata = automation_memory.get("metadata", {})
        status["automation_memory"] = {
            "ready": automation_memory.get("ready"),
            "status": automation_memory.get("status"),
            "message": automation_memory.get("message"),
            "readiness": automation_metadata.get("readiness"),
            "probe": automation_metadata.get("probe"),
            "search": {
                "available": automation_metadata.get("search", {}).get("available"),
                "status": automation_metadata.get("search", {}).get("status"),
            },
            "recall": {
                "events": automation_metadata.get("recall", {}).get("events"),
                "failure_rate": automation_metadata.get("recall", {}).get("failure_rate"),
            },
            "remote_sync": {
                "remote_configured": automation_metadata.get("remote_sync", {}).get("remote_configured"),
                "pending_retry_count": automation_metadata.get("remote_sync", {}).get("pending_retry_count"),
            },
        }
        sections["status"] = status
        if warning and all(warning not in block for block in content_blocks):
            content_blocks.append(warning)
        content_blocks.append(
            f"[vault] Status: {status['note_count']} notes, qmd {'available' if status['qmd_available'] else 'unavailable'}"
        )

        queue = _queue_capture_status(vault)
        queued_capture_count = int(queue["queued_capture_count"])
        sections["queue"] = queue
        if queued_capture_count:
            suffix = ""
            if queue.get("queued_capture_count_source") == "legacy_fallback":
                suffix = " (legacy fallback)"
            elif queue.get("queued_capture_count_source") == "current_plus_legacy":
                suffix = " (includes legacy queue)"
            content_blocks.append(f"[vault] Capture queue: {queued_capture_count} queued pi capture(s){suffix}")

    if include_tool_context_preview:
        sections["tool_context_preview"] = {
            "available": False,
            "reason": "file_path_required",
            "hint": "Call memento_tool_context with a concrete file path when handling read-tool results.",
        }

    raw_content = "\n\n".join(block for block in content_blocks if block).strip()
    content, truncated = _truncate_session_context(raw_content, char_budget)
    expandable_paths = _unique_result_paths(raw_results)
    budget_notes = []
    if truncated:
        budget_notes.append("content truncated to token_budget; use expandable_paths with memento_get for full notes")

    payload = {
        "should_inject": bool(content),
        "content": content,
        "source": "session-context",
        "sections": sections,
        "results": [_compact_session_result(result) for result in raw_results],
        "metadata": {
            "cwd": cwd,
            "session_id": session_id,
            "host_id": host_id,
            "token_budget": token_budget,
            "char_budget": char_budget,
            "packet_char_budget": packet_char_budget,
            "used_chars": len(content),
            "truncated": truncated,
            "expandable_paths": expandable_paths,
            "warnings": warnings,
            "budget_notes": budget_notes,
        },
    }
    payload = _fit_session_context_payload(payload, packet_char_budget)
    if recall_top_path and recall_content_marker and recall_content_marker in payload.get("content", ""):
        record_recall([recall_top_path], session_id, cwd=cwd, host_id=host_id)
    log_automation_memory_health(
        "packet_success",
        source="session-context",
        should_inject=bool(payload.get("should_inject")),
        result_count=len(payload.get("results") or []),
        warning_count=len(payload.get("metadata", {}).get("warnings", [])),
        truncated=bool(payload.get("metadata", {}).get("truncated")),
    )
    return payload


RECALL_DEDUP_PATH = os.path.join(RUNTIME_DIR, "recall-dedup.json")
DEFERRED_BRIEFING_PATH = os.path.join(RUNTIME_DIR, "deferred-briefing.json")
DEFERRED_BRIEFING_TTL_SECONDS = 60
DEEP_RECALL_PENDING_PATH = os.path.join(RUNTIME_DIR, "deep-recall-pending.json")

CACHE_PATH = os.path.join(RUNTIME_DIR, "tool-context-cache.json")

SKIP_PREFIXES = (
    "/usr/",
    "/etc/",
    "/proc/",
    "/sys/",
    "/dev/",
    "/tmp/",
    "/var/",
    "/snap/",
)

SKIP_SEGMENTS = {
    "node_modules",
    ".git",
    ".pi",
    "dist",
    "build",
    ".next",
    "__pycache__",
    ".cache",
    "vendor",
    ".terraform",
    "target",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "coverage",
    ".nyc_output",
}

SKIP_EXTENSIONS = {
    ".json",
    ".lock",
    ".yaml",
    ".yml",
    ".toml",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".map",
    ".min.js",
    ".min.css",
    ".sum",
    ".mod",
    ".csv",
    ".xml",
    ".sql",
    ".env",
    ".pem",
    ".key",
    ".crt",
}

SKIP_FILENAMES = {
    "SKILL.md",
    "MEMORY.md",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "tsconfig.json",
    "tsconfig.base.json",
    "go.mod",
    "go.sum",
    "Cargo.lock",
    "Cargo.toml",
    ".gitignore",
    ".prettierrc",
    ".eslintrc",
    ".eslintrc.js",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "jest.config.js",
    "jest.config.ts",
    "vitest.config.ts",
    ".env",
    ".env.local",
    ".env.example",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
}

STOP_SEGMENTS = {
    "src",
    "lib",
    "app",
    "apps",
    "cmd",
    "pkg",
    "internal",
    "components",
    "utils",
    "hooks",
    "helpers",
    "services",
    "test",
    "tests",
    "__tests__",
    "spec",
    "specs",
    "pages",
    "views",
    "controllers",
    "models",
    "resolvers",
    "middleware",
    "handlers",
    "routes",
    "api",
    "common",
    "shared",
    "core",
    "config",
    "types",
    "frontend",
    "backend",
    "server",
    "client",
}


def should_skip_tool_context_path(file_path: str) -> bool:
    """Fast exit checks for file reads that should not receive vault context."""
    if any(file_path.startswith(prefix) for prefix in SKIP_PREFIXES):
        return True

    path = Path(file_path)
    parts = path.parts
    if path.name == "memento.ts" and "extensions" in parts:
        return True
    if path.name == "pi_bridge.py" and "memento" in parts:
        return True

    vault = get_vault()
    try:
        resolved_path = Path(os.path.realpath(file_path))
        resolved_vault = Path(os.path.realpath(vault))
        if resolved_path == resolved_vault or resolved_vault in resolved_path.parents:
            return True
    except (OSError, ValueError):
        pass

    if any(part in SKIP_SEGMENTS for part in parts):
        return True

    if path.suffix.lower() in SKIP_EXTENSIONS:
        return True
    return path.name in SKIP_FILENAMES


def extract_tool_context_keywords(file_path: str, cwd: str = "") -> str:
    """Extract searchable keywords from a file path for BM25 query.

    File-read hooks pass absolute normalized paths, but BM25 queries should be
    about the code area, not the user's checkout/worktree scaffolding. Prefer a
    path relative to the session cwd when possible, then fall back to stripping
    the home directory.
    """
    path = file_path
    if cwd:
        try:
            rel = os.path.relpath(os.path.realpath(file_path), os.path.realpath(os.path.expanduser(cwd)))
            if rel and not rel.startswith("..") and not os.path.isabs(rel):
                path = rel
        except (OSError, ValueError):
            pass

    home = str(Path.home())
    if path.startswith(home):
        path = path[len(home) :]

    words = []
    for part in Path(path).parts:
        if part.startswith(".") or part in STOP_SEGMENTS:
            continue
        if part.endswith(".git"):
            part = part[:-4]
        if "." in part and part != part.split(".")[0]:
            part = Path(part).stem
        for token in re.split(r"[-_./]", part):
            for word in re.sub(r"([a-z])([A-Z])", r"\1 \2", token).split():
                normalized = word.lower().strip()
                if len(normalized) > 1:
                    words.append(normalized)

    seen = set()
    unique = []
    for word in words:
        if word not in seen:
            seen.add(word)
            unique.append(word)
    return " ".join(unique)


# v2: caches written before the relative-path cwd fix hold dir entries
# poisoned by paths resolved against the wrong project; drop them once.
# v3: dir entries become project-scoped ("<cwd>::<dir>") and carry a ts for
# TTL expiry; pre-v3 keys are shape-incompatible, so drop them once.
# v4: query extraction became cwd-relative; old dir entries may be poisoned by
# checkout/worktree path terms and must be recomputed.
# v5: injection/session state and directory entries are scoped by host/project,
# writes are locked/merged, and stale/bulky entries are pruned.
TOOL_CONTEXT_CACHE_SCHEMA = 5
TOOL_CONTEXT_CACHE_MAX_DIRS = 500
TOOL_CONTEXT_CACHE_MAX_INJECTION_SCOPES = 100
TOOL_CONTEXT_INJECTION_TTL_HOURS = 48


def _empty_tool_context_cache() -> dict:
    return {"schema": TOOL_CONTEXT_CACHE_SCHEMA, "dirs": {}, "last_qmd_call": 0, "injections": {}}


def _tool_context_cache_ttl_hours(config: dict | None = None) -> float:
    try:
        return float((config or get_config()).get("tool_context_cache_ttl_hours", 24))
    except (TypeError, ValueError):
        return 24.0


def _prune_tool_context_cache(cache: dict, config: dict | None = None) -> dict:
    normalized = _empty_tool_context_cache()
    if not isinstance(cache, dict):
        return normalized
    normalized["last_qmd_call"] = cache.get("last_qmd_call", 0)

    ttl_hours = _tool_context_cache_ttl_hours(config)
    now = time.time()
    dirs = cache.get("dirs", {}) if isinstance(cache.get("dirs"), dict) else {}
    kept_dirs = {}
    for key, entry in dirs.items():
        if not isinstance(entry, dict):
            continue
        if ttl_hours > 0:
            try:
                if now - float(entry.get("ts", 0)) >= ttl_hours * 3600:
                    continue
            except (TypeError, ValueError):
                continue
        kept_dirs[str(key)] = entry
    if len(kept_dirs) > TOOL_CONTEXT_CACHE_MAX_DIRS:
        newest = sorted(kept_dirs.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)
        kept_dirs = dict(newest[:TOOL_CONTEXT_CACHE_MAX_DIRS])
    normalized["dirs"] = kept_dirs

    injection_ttl = float((config or {}).get("tool_context_injection_ttl_hours", TOOL_CONTEXT_INJECTION_TTL_HOURS))
    injections = cache.get("injections", {}) if isinstance(cache.get("injections"), dict) else {}
    kept_injections = {}
    cutoff = now - injection_ttl * 3600 if injection_ttl > 0 else None
    for key, entry in injections.items():
        if not isinstance(entry, dict):
            continue
        updated = entry.get("updated")
        if updated is not None and cutoff is not None:
            try:
                if float(updated) < cutoff:
                    continue
            except (TypeError, ValueError):
                continue
        paths = [str(path) for path in entry.get("paths", []) if path]
        kept_injections[str(key)] = {
            "count": max(int(entry.get("count", 0) or 0), len(paths)),
            "paths": paths,
            "updated": float(updated or now),
            "scope": entry.get("scope", {}),
        }
    if len(kept_injections) > TOOL_CONTEXT_CACHE_MAX_INJECTION_SCOPES:
        newest = sorted(kept_injections.items(), key=lambda kv: kv[1].get("updated", 0), reverse=True)
        kept_injections = dict(newest[:TOOL_CONTEXT_CACHE_MAX_INJECTION_SCOPES])
    normalized["injections"] = kept_injections
    return normalized


def _merge_tool_context_cache(existing: dict, incoming: dict, config: dict | None = None) -> dict:
    merged = _prune_tool_context_cache(existing, config)
    incoming = _prune_tool_context_cache(incoming, config)
    merged["last_qmd_call"] = max(
        float(merged.get("last_qmd_call", 0) or 0), float(incoming.get("last_qmd_call", 0) or 0)
    )
    dirs = merged.setdefault("dirs", {})
    for key, entry in incoming.get("dirs", {}).items():
        current = dirs.get(key)
        if not current:
            dirs[key] = entry
            continue
        try:
            incoming_ts = float(entry.get("ts", 0) or 0)
        except (TypeError, ValueError):
            incoming_ts = 0.0
        try:
            current_ts = float(current.get("ts", 0) or 0)
        except (TypeError, ValueError):
            current_ts = 0.0
        if incoming_ts >= current_ts:
            dirs[key] = entry
    for key, entry in incoming.get("injections", {}).items():
        current = merged.setdefault("injections", {}).get(key)
        if not current:
            merged["injections"][key] = entry
            continue
        paths = []
        seen = set()
        for path in list(current.get("paths", [])) + list(entry.get("paths", [])):
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
        current["paths"] = paths
        current["count"] = max(int(current.get("count", 0) or 0), int(entry.get("count", 0) or 0), len(paths))
        current["updated"] = max(float(current.get("updated", 0) or 0), float(entry.get("updated", 0) or 0))
        current["scope"] = current.get("scope") or entry.get("scope", {})
    return _prune_tool_context_cache(merged, config)


def load_cache() -> dict:
    """Load the tool-context cache from disk under a best-effort file lock."""
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, "a+") as f:
                _lock_file(f)
                f.seek(0)
                raw = f.read()
            cache = json.loads(raw) if raw.strip() else {}
            if cache.get("schema") != TOOL_CONTEXT_CACHE_SCHEMA:
                return _prune_tool_context_cache(
                    {
                        "schema": TOOL_CONTEXT_CACHE_SCHEMA,
                        "dirs": {},
                        "last_qmd_call": cache.get("last_qmd_call", 0) if isinstance(cache, dict) else 0,
                        # Pre-v5 injection entries were keyed only by session id;
                        # preserving them would let one host/project suppress another.
                        "injections": {},
                    }
                )
            return _prune_tool_context_cache(cache)
    except (json.JSONDecodeError, OSError):
        pass
    return _empty_tool_context_cache()


def save_cache(cache: dict) -> None:
    """Merge and write the tool-context cache under an exclusive file lock."""
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "a+") as f:
            _lock_file(f)
            f.seek(0)
            raw = f.read()
            try:
                existing = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                existing = {}
            state = _merge_tool_context_cache(existing, cache)
            f.seek(0)
            f.truncate()
            json.dump(state, f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    except OSError:
        pass


def _tool_context_dir_key(cwd: str, normalized_path: str, host_id: str | None = None) -> str:
    """Scope cache entries by host/project (cwd) as well as directory.

    A directory shared between checkouts (or a mis-resolved path) must not
    replay one project's cached results into another project's sessions.
    """
    try:
        scope = os.path.realpath(os.path.expanduser(cwd)).rstrip("/") if cwd else "-"
    except (OSError, ValueError):
        scope = "-"
    return f"{runtime_host_id(host_id)}::{scope}::{Path(normalized_path).parent}"


def _tool_context_entry_fresh(entry: dict, config: dict) -> bool:
    try:
        ttl_hours = float(config.get("tool_context_cache_ttl_hours", 24))
    except (TypeError, ValueError):
        ttl_hours = 24.0
    if ttl_hours <= 0:
        return True  # TTL disabled
    return (time.time() - float(entry.get("ts", 0))) < ttl_hours * 3600


def get_recall_paths(
    session_id: str = "unknown",
    cwd: str = "",
    project_slug: str | None = None,
    host_id: str | None = None,
) -> set[str]:
    """Paths recently injected by prompt recall in this scoped session."""
    return recently_injected_paths(session_id, cwd=cwd, project_slug=project_slug, host_id=host_id)


def _tool_context_session_key(
    session_id: str = "unknown", cwd: str = "", host_id: str | None = None
) -> tuple[str, dict]:
    scope = _runtime_scope(session_id, cwd, host_id=host_id)
    return _scope_storage_key(scope), scope


def session_injection_count(cache: dict, session_id: str, cwd: str = "", host_id: str | None = None) -> int:
    key, _scope = _tool_context_session_key(session_id, cwd, host_id)
    injections = cache.get("injections", {})
    entry = injections.get(key, {})
    return entry.get("count", 0)


def session_injected_paths(cache: dict, session_id: str, cwd: str = "", host_id: str | None = None) -> set[str]:
    key, _scope = _tool_context_session_key(session_id, cwd, host_id)
    injections = cache.get("injections", {})
    entry = injections.get(key, {})
    return set(entry.get("paths", []))


def record_injection(
    cache: dict, session_id: str, note_paths: list[str], cwd: str = "", host_id: str | None = None
) -> None:
    if "injections" not in cache:
        cache["injections"] = {}
    key, scope = _tool_context_session_key(session_id, cwd, host_id)
    if key not in cache["injections"]:
        cache["injections"][key] = {"count": 0, "paths": [], "scope": scope, "updated": time.time()}

    entry = cache["injections"][key]
    entry["scope"] = scope
    entry["updated"] = time.time()
    entry["count"] += len(note_paths)
    entry["paths"].extend(note_paths)


def strip_injection(text: str) -> str:
    """Strip instruction-like patterns from injected content."""
    if not text:
        return text
    text = re.sub(r"(?i)(ignore\s+(all\s+)?previous\s+instructions)", "[filtered]", text)
    text = re.sub(r"(?i)(you\s+are\s+now\s+|you\s+must\s+now\s+)", "[filtered]", text)
    text = re.sub(r"(?i)^(system|assistant)\s*:", "[filtered]:", text)
    text = re.sub(r"</?s>", "", text)
    return text


def format_tool_context_result(result: dict) -> str:
    """Format a QMD result as a compact one-liner."""
    title = strip_injection(result.get("title", ""))
    snippet = strip_injection(result.get("snippet", "").strip())

    if snippet:
        dot = snippet.find(".")
        if 0 < dot < 100:
            snippet = snippet[: dot + 1]
        elif len(snippet) > 100:
            snippet = snippet[:100] + "..."

    line = f"  - {title}"
    if snippet:
        line += f": {snippet}"
    return line


def tool_context_diagnostics_enabled(config: dict) -> bool:
    """Return whether retrieval logs should include tool-context decisions."""
    return bool(config.get("tool_context_diagnostics", True))


def log_tool_context_decision(
    config: dict,
    decision: str,
    metadata: dict,
    *,
    candidates: list[dict] | None = None,
    **kwargs,
) -> None:
    """Log one terminal tool-context decision for diagnostic analysis.

    ``log_retrieval`` still controls whether anything is written (via
    retrieval_log/MEMENTO_DEBUG). This helper makes the tool-context path
    measurable when diagnostic logging is enabled: every call emits a terminal
    decision, and optional candidate summaries explain why a note did or did
    not survive gating.
    """
    if not tool_context_diagnostics_enabled(config):
        return

    payload = {
        "decision": decision,
        "reason": decision,
        "cwd": metadata.get("cwd", ""),
        "session_id": metadata.get("session_id", ""),
        "tool_name": metadata.get("tool_name", ""),
        "file_path": metadata.get("file_path", ""),
    }
    if metadata.get("lineage_id"):
        payload["lineage_id"] = metadata.get("lineage_id")
    if metadata.get("query"):
        payload["query"] = metadata.get("query")
    payload.update(kwargs)

    if candidates is not None and config.get("tool_context_diagnostics_include_candidates", False):
        max_candidates = int(config.get("tool_context_diagnostics_max_candidates", 10) or 10)
        payload["candidates"] = [_candidate_summary(result) for result in candidates[: max(0, max_candidates)]]

    log_retrieval("tool-context", "decision", **payload)


def build_tool_context(
    tool_name: str,
    file_path: str,
    cwd: str = "",
    session_id: str = "unknown",
    lineage_id: str | None = None,
    host_id: str | None = None,
) -> LifecycleResult:
    """Build context for a file-read tool result.

    lineage_id, when provided, keys the per-session injection caps instead of
    session_id: resumed sessions get fresh session ids but share a transcript,
    and caps keyed on the transient id reset on every resume.
    """
    config = get_config()
    host_id = runtime_host_id(host_id)
    lineage = lineage_id or session_id
    metadata = {
        "cwd": cwd,
        "session_id": session_id,
        "host_id": host_id,
        "tool_name": tool_name,
        "file_path": file_path,
    }
    if lineage_id:
        metadata["lineage_id"] = lineage_id

    def no_context(reason: str, **diagnostics) -> LifecycleResult:
        log_tool_context_decision(config, reason, metadata, **diagnostics)
        return LifecycleResult(False, "", "tool-context", reason=reason, metadata=metadata)

    if not config.get("tool_context", True):
        return no_context("disabled")
    if tool_name not in {"Read", "read"}:
        return no_context("unsupported-tool")
    if not file_path:
        return no_context("missing-file-path")

    try:
        expanded = os.path.expanduser(file_path)
        # Hosts like the Pi bridge pass tool paths relative to the user's
        # project while this process runs with an unrelated cwd; resolving
        # against the process cwd produced wrong-project queries. Anchor
        # relative paths to the session cwd instead.
        if cwd and not os.path.isabs(expanded):
            expanded = os.path.join(os.path.expanduser(cwd), expanded)
        normalized_path = os.path.realpath(expanded)
    except (OSError, ValueError):
        return no_context("invalid-file-path")
    metadata["file_path"] = normalized_path

    if should_skip_tool_context_path(normalized_path):
        return no_context("skipped-path")

    if not has_qmd():
        return no_context("qmd-unavailable")

    cache = load_cache()
    max_injections = config.get("tool_context_max_injections", 5)
    if session_injection_count(cache, lineage, cwd=cwd, host_id=host_id) >= max_injections:
        return no_context("cap-reached", max_injections=max_injections)

    dir_key = _tool_context_dir_key(cwd, normalized_path, host_id=host_id)
    search_query = None
    latency_ms = 0
    source = "search"
    raw_result_count = None
    enhanced_result_count = None
    cached_entry = cache.get("dirs", {}).get(dir_key)
    if cached_entry is not None and not _tool_context_entry_fresh(cached_entry, config):
        del cache["dirs"][dir_key]
        cached_entry = None
        log_retrieval("tool-context", "cache-expired", dir_key=dir_key)
    if cached_entry is not None:
        source = "cache"
        results = cached_entry.get("results", [])
        search_query = cached_entry.get("query")
        raw_result_count = cached_entry.get("raw_result_count")
        enhanced_result_count = cached_entry.get("enhanced_result_count")
        if search_query:
            metadata["query"] = search_query
        if not results:
            return no_context("no-results", dir_key=dir_key, source=source)
        log_retrieval("tool-context", "cache-hit", file_path=normalized_path, dir_key=dir_key)
    else:
        cooldown = config.get("tool_context_cooldown", 3)
        last_call = cache.get("last_qmd_call", 0)
        if time.time() - last_call < cooldown:
            return no_context("cooldown", cooldown=cooldown, seconds_since_last_call=round(time.time() - last_call, 3))

        search_query = extract_tool_context_keywords(normalized_path, cwd)
        metadata["query"] = search_query
        if not search_query or len(search_query.split()) < 2:
            cache.setdefault("dirs", {})[dir_key] = {"results": [], "ts": time.time(), "query": search_query}
            save_cache(cache)
            return no_context("insufficient-keywords", dir_key=dir_key, query=search_query)

        min_score = config.get("tool_context_min_score", 0.75)
        max_notes = config.get("tool_context_max_notes", 2)
        t0 = time.time()
        try:
            raw_results = qmd_search_with_extras(
                search_query,
                limit=max_notes + 5,
                semantic=False,
                timeout=2,
                min_score=min_score,
            )
            raw_result_count = len(raw_results)
            # Tool-context is unsolicited injection: require a positive project
            # match instead of letting untagged notes through as general knowledge.
            results = enhance_results(raw_results, config, cwd=cwd, require_project_match=True)
            enhanced_result_count = len(results)
        except Exception as exc:
            latency_ms = int((time.time() - t0) * 1000)
            cache["last_qmd_call"] = time.time()
            save_cache(cache)
            log_retrieval(
                "tool-context",
                "backend-error",
                query=search_query,
                file_path=normalized_path,
                latency_ms=latency_ms,
                error_type=type(exc).__name__,
            )
            return no_context(
                "backend-error",
                dir_key=dir_key,
                source=source,
                latency_ms=latency_ms,
                error_type=type(exc).__name__,
                min_score=min_score,
            )
        latency_ms = int((time.time() - t0) * 1000)

        cache["last_qmd_call"] = time.time()
        cache.setdefault("dirs", {})[dir_key] = {
            "results": results,
            "ts": time.time(),
            "query": search_query,
            "raw_result_count": raw_result_count,
            "enhanced_result_count": enhanced_result_count,
        }
        save_cache(cache)

        if not results:
            log_retrieval(
                "tool-context",
                "no-results",
                query=search_query,
                file_path=normalized_path,
                latency_ms=latency_ms,
            )
            return no_context(
                "no-results",
                dir_key=dir_key,
                source=source,
                latency_ms=latency_ms,
                raw_result_count=raw_result_count,
                enhanced_result_count=enhanced_result_count,
                min_score=min_score,
            )

    recall_paths = get_recall_paths(session_id, cwd=cwd, host_id=host_id)
    already_injected = session_injected_paths(cache, lineage, cwd=cwd, host_id=host_id)
    exclude = recall_paths | already_injected
    filtered = [r for r in results if r.get("path", "") not in exclude]
    if not filtered:
        return no_context(
            "duplicate",
            dir_key=dir_key,
            source=source,
            result_count=len(results),
            recall_excluded_count=len(recall_paths),
            session_excluded_count=len(already_injected),
        )

    max_notes = config.get("tool_context_max_notes", 2)
    selected = filtered[:max_notes]
    lines = ["[connected-to-vault]"]
    injected_paths = []
    for result in selected:
        lines.append(format_tool_context_result(result))
        injected_paths.append(result.get("path", ""))

    injected_text = "\n".join(lines)
    injected_titles = [r.get("title", "") for r in selected]
    log_retrieval(
        "tool-context",
        "inject",
        file_path=normalized_path,
        query=search_query or dir_key,
        injected_titles=injected_titles,
        injected_paths=injected_paths,
        injected_chars=len(injected_text),
        latency_ms=latency_ms,
    )
    record_access_hits("tool-context", "inject", selected, query=search_query or dir_key, session_id=session_id)
    log_tool_context_decision(
        config,
        "injected",
        metadata,
        candidates=filtered,
        dir_key=dir_key,
        source=source,
        result_count=len(results),
        filtered_count=len(filtered),
        selected_count=len(selected),
        injected_titles=injected_titles,
        injected_paths=injected_paths,
        injected_chars=len(injected_text),
        latency_ms=latency_ms,
        raw_result_count=raw_result_count,
        enhanced_result_count=enhanced_result_count,
    )

    record_injection(cache, lineage, injected_paths, cwd=cwd, host_id=host_id)
    save_cache(cache)
    return LifecycleResult(
        should_inject=True,
        content=injected_text,
        source="tool-context",
        results=selected,
        metadata=metadata,
    )
