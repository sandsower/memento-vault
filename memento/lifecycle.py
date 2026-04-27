"""Shared lifecycle retrieval primitives for memento host adapters."""

from __future__ import annotations

import json
import os
import re
import subprocess as _subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from memento.config import RUNTIME_DIR, detect_project, get_config, get_vault
from memento.graph import load_or_build_graph, lookup_concepts, lookup_project_notes
from memento.llm import llm_complete
from memento.search import (
    enhance_results,
    has_qmd,
    is_vsearch_warm,
    mark_vsearch_warm,
    multi_hop_search,
    prf_expand_query,
    qmd_search,
    qmd_search_with_extras,
    rrf_fuse,
)
from memento.store import RETRIEVAL_LOG_PATH, log_retrieval
from memento.utils import read_hook_input

TRIAGE_HEALTH_WINDOW_HOURS = 24
TRIAGE_HEALTH_MIN_EVENTS = 3
TRIAGE_HEALTH_FAIL_RATIO = 0.5


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


def empty_result(source: str, reason: str = "no-results") -> LifecycleResult:
    return LifecycleResult(
        should_inject=False,
        content="",
        source=source,
        reason=reason,
    )


def triage_health_warning():
    """Return a one-line warning if SessionEnd triage is silently failing.

    Scans retrieval.jsonl for the last 24h of triage events and flags when
    the failure ratio crosses TRIAGE_HEALTH_FAIL_RATIO. Returns None when
    healthy or when there isn't enough data to judge.
    """
    try:
        from datetime import datetime, timedelta

        if not os.path.exists(RETRIEVAL_LOG_PATH):
            return None

        cutoff = datetime.now() - timedelta(hours=TRIAGE_HEALTH_WINDOW_HOURS)
        total = 0
        failed = 0
        with open(RETRIEVAL_LOG_PATH) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("hook") != "triage":
                    continue
                ts_raw = rec.get("ts")
                if not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_raw)
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                action = rec.get("action") or ""
                if action not in ("decision", "parse_transcript_failed", "structured_notes_llm_failed"):
                    continue
                total += 1
                if action != "decision":
                    failed += 1

        if total < TRIAGE_HEALTH_MIN_EVENTS:
            return None
        if (failed / total) < TRIAGE_HEALTH_FAIL_RATIO:
            return None
        return f"[vault] WARN: triage failing {failed}/{total} in last {TRIAGE_HEALTH_WINDOW_HOURS}h — check {RETRIEVAL_LOG_PATH}"
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


def spawn_deferred_search(project_slug, git_branch, linked_notes, config):
    """Spawn a background subprocess to run QMD search and write results."""
    max_notes = config.get("briefing_max_notes", 5)
    min_score = config.get("briefing_min_score", 0.3)

    # Build search query
    query_parts = [project_slug.replace("-", " ")]
    if git_branch and git_branch not in ("main", "master", "HEAD"):
        branch_words = git_branch.replace("-", " ").replace("/", " ")
        query_parts.append(branch_words)

    # Write the search params for the background worker
    params = {
        "query": " ".join(query_parts),
        "max_notes": max_notes,
        "min_score": min_score,
        "linked_notes": linked_notes,
        "cwd": config.get("_cwd", ""),
        "timestamp": time.time(),
    }

    try:
        with open(DEFERRED_BRIEFING_PATH, "w") as f:
            json.dump({"status": "pending", "params": params}, f)

        # Spawn background worker — the same script with --deferred flag
        _subprocess.Popen(
            [sys.executable, str(Path(__file__).parent.parent / "hooks" / "vault-briefing.py"), "--deferred"],
            stdin=_subprocess.DEVNULL,
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        # If spawn fails, clean up so recall doesn't wait for stale pending
        try:
            os.unlink(DEFERRED_BRIEFING_PATH)
        except OSError:
            pass


def run_deferred_briefing_search():
    """Background worker: run QMD search and write results to the deferred file."""
    try:
        with open(DEFERRED_BRIEFING_PATH) as f:
            data = json.load(f)

        if data.get("status") != "pending":
            sys.exit(0)

        params = data["params"]
        query = params["query"]
        max_notes = params["max_notes"]
        min_score = params["min_score"]
        linked_notes = params.get("linked_notes", [])

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

        for result in results:
            title = result.get("title", "")
            if title in seen:
                continue
            seen.add(title)
            note_lines.append(format_qmd_result(result))

        for note_name in linked_notes:
            if note_name in seen or len(note_lines) >= max_notes:
                break
            seen.add(note_name)
            oneliner = read_note_oneliner(note_name)
            if oneliner:
                note_lines.append(f"  - {oneliner}")

        final_notes = note_lines[:max_notes]
        with open(DEFERRED_BRIEFING_PATH, "w") as f:
            json.dump(
                {
                    "status": "ready",
                    "note_lines": final_notes,
                    "timestamp": time.time(),
                },
                f,
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
            os.unlink(DEFERRED_BRIEFING_PATH)
        except OSError:
            pass


def run_remote_briefing(cwd, config):
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

        with open(DEFERRED_BRIEFING_PATH, "w") as f:
            json.dump({"status": "ready", "note_lines": note_lines, "timestamp": time.time(), "source": "remote"}, f)

    return summary


def build_briefing(cwd: str, session_id: str = "unknown") -> LifecycleResult:
    """Build session-start briefing content."""
    config = get_config()
    metadata = {"cwd": cwd, "session_id": session_id}

    def no_briefing(reason: str) -> LifecycleResult:
        return LifecycleResult(False, "", "briefing", reason=reason, metadata=metadata)

    if not config.get("session_briefing", True):
        return no_briefing("disabled")
    if not cwd:
        return no_briefing("missing-cwd")

    from memento.remote_client import is_remote

    if is_remote():
        try:
            if os.path.exists(DEFERRED_BRIEFING_PATH):
                os.unlink(DEFERRED_BRIEFING_PATH)
            remote_content = run_remote_briefing(cwd, config)
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

    warning = triage_health_warning()
    if warning:
        lines.append(warning)

    if config.get("project_maps_enabled", True) and has_qmd():
        try:
            max_notes = config.get("briefing_max_notes", 5)
            map_notes = lookup_project_notes(project_slug, limit=max_notes)
            if len(map_notes) >= max_notes:
                note_lines = []
                for note in map_notes[:max_notes]:
                    title = note.get("title", "")
                    note_lines.append(f"  - {title}")
                with open(DEFERRED_BRIEFING_PATH, "w") as f:
                    json.dump(
                        {
                            "status": "ready",
                            "note_lines": note_lines,
                            "timestamp": time.time(),
                            "source": "project-maps",
                        },
                        f,
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

    if has_qmd():
        config["_cwd"] = cwd
        spawn_deferred_search(project_slug, git_branch, linked_notes, config)

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


def should_append_project_to_recall(prompt: str) -> bool:
    """Only append project slug when the prompt has enough standalone signal."""
    return not is_low_signal_recall_prompt(prompt)


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


def should_skip_recall(prompt, config):
    """Relevance gate — returns True if we should skip vault injection."""
    prompt = prompt.strip()

    # Too short
    if len(prompt) < 10:
        return True

    if config.get("recall_skip_low_signal", True) and is_low_signal_recall_prompt(prompt):
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


def is_duplicate(top_result_path):
    """Check if the top result is the same as the last injection.
    Returns True if we should skip to avoid repetition.
    """
    try:
        if not os.path.exists(LAST_RECALL_PATH):
            return False

        with open(LAST_RECALL_PATH) as f:
            last = json.load(f)

        # Skip if same top result within the last 3 prompts
        if last.get("top_path") == top_result_path:
            prompts_since = last.get("prompts_since", 0)
            if prompts_since < 3:
                # Update counter
                last["prompts_since"] = prompts_since + 1
                with open(LAST_RECALL_PATH, "w") as f:
                    json.dump(last, f)
                return True

        return False

    except Exception:
        return False


def record_recall(top_result_path):
    """Record this recall for dedup tracking."""
    try:
        with open(LAST_RECALL_PATH, "w") as f:
            json.dump(
                {
                    "top_path": top_result_path,
                    "prompts_since": 0,
                    "timestamp": time.time(),
                },
                f,
            )
    except Exception:
        pass


def bump_prompts_since():
    """Increment the prompts_since counter when we skip injection."""
    try:
        if not os.path.exists(LAST_RECALL_PATH):
            return

        with open(LAST_RECALL_PATH) as f:
            last = json.load(f)

        last["prompts_since"] = last.get("prompts_since", 0) + 1
        with open(LAST_RECALL_PATH, "w") as f:
            json.dump(last, f)

    except Exception:
        pass


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


def consume_deferred_briefing():
    """Check for deferred briefing from SessionStart and consume it.

    Returns formatted lines to prepend, or empty list.
    If the background search is still pending, leaves the file intact
    so the next prompt can pick it up. Only deletes on successful
    consumption or if the file is stale (>60s).
    """
    try:
        if not os.path.exists(DEFERRED_BRIEFING_PATH):
            return []

        with open(DEFERRED_BRIEFING_PATH) as f:
            data = json.load(f)

        status = data.get("status", "")

        if status == "pending":
            # Background worker still running — check staleness
            ts = data.get("params", {}).get("timestamp", 0)
            if ts and (time.time() - ts) > 60:
                # Stale pending file — worker probably crashed
                os.unlink(DEFERRED_BRIEFING_PATH)
            # Either way, nothing to inject yet
            return []

        if status != "ready":
            os.unlink(DEFERRED_BRIEFING_PATH)
            return []

        # Got results — consume and clean up
        note_lines = data.get("note_lines", [])
        os.unlink(DEFERRED_BRIEFING_PATH)

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
            os.unlink(DEFERRED_BRIEFING_PATH)
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
                str(Path(__file__).parent.parent / "hooks" / "vault-recall.py"),
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


def run_remote_recall(prompt, cwd, config):
    """Run recall via the remote vault client. Returns (lines, top_path) or ([], None)."""
    from memento.remote_client import search as remote_search

    if should_skip_recall(prompt, config):
        return [], None

    max_notes = config.get("recall_max_notes", 3)
    min_score = config.get("recall_min_score", 0.4)

    results = remote_search(query=prompt, limit=max_notes + 3, min_score=min_score, cwd=cwd)
    if not results:
        return [], None

    top_path = results[0].get("path", "")
    if is_duplicate(top_path):
        return [], None

    lines = ["[vault] Related memories:"]
    for result in results[:max_notes]:
        lines.append(format_result(result))

    return lines, top_path


def _run_recall_lines(prompt: str, cwd: str = "", session_id: str = "unknown"):
    """Run the recall search. Returns (lines, top_path, results, reason)."""
    config = get_config()
    project_slug = "unknown"
    if cwd:
        try:
            project_slug, _ = detect_project(cwd, None)
        except Exception:
            project_slug = "unknown"

    log_recall_diagnostic(
        config,
        "start",
        prompt_len=len(prompt or ""),
        cwd=cwd,
        session_id=session_id,
        project_slug=project_slug,
        signal_terms=recall_signal_terms(prompt or ""),
        low_signal=is_low_signal_recall_prompt(prompt or ""),
    )

    if not config.get("prompt_recall", True):
        log_recall_diagnostic(config, "decision", decision="skipped", reason="disabled")
        return [], None, [], "disabled"
    if not prompt:
        log_recall_diagnostic(config, "decision", decision="skipped", reason="empty-prompt")
        return [], None, [], "empty-prompt"
    if should_skip_recall(prompt, config):
        bump_prompts_since()
        reason = (
            "low-signal-prompt"
            if config.get("recall_skip_low_signal", True) and is_low_signal_recall_prompt(prompt)
            else "skipped-prompt"
        )
        log_retrieval("recall", reason, query=prompt, cwd=cwd, session_id=session_id)
        log_recall_diagnostic(config, "skip", reason=reason, normalized_prompt=re.sub(r"\s+", " ", prompt).strip())
        log_recall_diagnostic(config, "decision", decision="skipped", reason=reason)
        return [], None, [], reason

    # Try remote vault first (has cross-device data), fall through to local
    from memento.remote_client import is_remote

    if is_remote() and prompt:
        try:
            lines, top_path = run_remote_recall(prompt, cwd, config)
            if lines:
                log_recall_diagnostic(config, "decision", decision="injected", source="remote", top_path=top_path)
                return lines, top_path, [], None
        except Exception as exc:
            print(f"[memento] remote vault unreachable, using local only ({exc})", file=sys.stderr)

    vault = get_vault()
    if not vault.exists() or not (vault / "notes").exists():
        log_recall_diagnostic(config, "decision", decision="skipped", reason="vault-unavailable")
        return [], None, [], "vault-unavailable"

    if not has_qmd():
        log_recall_diagnostic(config, "decision", decision="skipped", reason="qmd-unavailable")
        return [], None, [], "qmd-unavailable"

    # BM25 search against the prompt, augmented with project context
    min_score = config.get("recall_min_score", 0.4)
    max_notes = config.get("recall_max_notes", 3)

    # Bias toward current project by appending project slug to query
    query = prompt
    appended_project = False
    if cwd and should_append_project_to_recall(prompt):
        if project_slug and project_slug != "unknown":
            query = f"{prompt} {project_slug.replace('-', ' ')}"
            appended_project = True
    log_recall_diagnostic(
        config,
        "query",
        original_prompt=prompt,
        final_query=query,
        appended_project=appended_project,
        project_slug=project_slug,
    )

    t0 = time.time()

    # Adaptive pipeline: BM25 first, decide depth based on confidence.
    #
    # Fast path (BM25 score >= threshold): BM25 + enhance_results only.
    #   This is the v1.1.0 path. ~800ms.
    #
    # Deep path (BM25 score < threshold): PRF expand + RRF fuse + CE rerank.
    #   PRF reuses initial results (no extra QMD call for term extraction).
    #   PRF expanded query runs one additional BM25 call.
    #   RRF adds one vsearch call (only when warm).
    #   CE reranks the fused results.
    #
    # The threshold is intentionally low (0.55) because BM25 scores for
    # natural language prompts against vault notes typically range 0.4-0.8.
    # At 0.55+, BM25 has found a reasonable match and the extra stages
    # add latency without much quality gain.

    high_conf = config.get("recall_high_confidence", 0.55)
    search_limit = max_notes + 4

    results = qmd_search_with_extras(
        query,
        limit=search_limit,
        semantic=False,
        timeout=5,
        min_score=min_score,
    )
    top_score = results[0]["score"] if results else 0
    pipeline_depth = "bm25"
    log_recall_candidates(config, results, "bm25", query=query)

    if top_score < high_conf and results:
        # Low confidence — try harder with PRF + RRF

        # PRF: expand query using terms from the results we already have (zero extra QMD calls)
        expanded_query = prf_expand_query(query, config=config, initial_results=results)
        if expanded_query != query:
            prf_results = qmd_search_with_extras(
                expanded_query,
                limit=search_limit,
                semantic=False,
                timeout=5,
                min_score=min_score,
            )
            if prf_results:
                existing = {r["path"] for r in results}
                for r in prf_results:
                    if r["path"] not in existing:
                        results.append(r)
                        existing.add(r["path"])
                results.sort(key=lambda r: r["score"], reverse=True)
                pipeline_depth = "prf"
                log_recall_candidates(config, results, "prf", query=expanded_query)

        # RRF: fuse with vsearch when warm
        if config.get("rrf_enabled", True) and is_vsearch_warm():
            vec_results = qmd_search_with_extras(
                query,
                limit=search_limit,
                semantic=True,
                timeout=5,
                min_score=min_score,
            )
            if vec_results:
                results = rrf_fuse([results, vec_results], k=config.get("rrf_k", 60))
                pipeline_depth = "rrf"
                log_recall_candidates(config, results, "rrf", query=query)

    latency_ms = int((time.time() - t0) * 1000)
    results_before = len(results)

    # Concept index supplement (always, O(1) lookup)
    if config.get("concept_index_enabled", True):
        try:
            concept_hits = lookup_concepts(prompt)
            if concept_hits:
                existing_paths = {r.get("path", "") for r in results}
                for hit in concept_hits:
                    if hit["path"] not in existing_paths:
                        hit["score"] = max(hit.get("score", 0), config.get("concept_index_score", 0.5))
                        results.append(hit)
                        existing_paths.add(hit["path"])
                log_recall_candidates(config, results, "concept-index", query=query)
        except Exception:
            pass

    # Multi-hop retrieval: follow wikilinks from top results
    multi_hop_gate = top_score < high_conf and config.get("multi_hop_enabled", False)
    multi_hop_added = 0
    if multi_hop_gate and results:
        try:
            pre_hop_count = len(results)
            results = multi_hop_search(prompt, results, config=config)
            multi_hop_added = len(results) - pre_hop_count
            pipeline_depth += "+hop"
            log_recall_candidates(config, results, "multi-hop", query=query)
        except Exception:
            pass

    # Deep recall: spawn background codex for complex prompts
    # Gate: low confidence AND feature enabled
    deep_recall_spawned = False
    if (
        top_score < high_conf
        and config.get("deep_recall_enabled", False)
        and results
        and not os.path.exists(DEEP_RECALL_PENDING_PATH)
    ):
        try:
            spawn_deep_recall(prompt, results, config)
            deep_recall_spawned = True
            pipeline_depth += "+deep"
        except Exception:
            pass

    if not results:
        bump_prompts_since()
        log_retrieval("recall", "no-results", query=query, latency_ms=latency_ms, pipeline=pipeline_depth)
        log_recall_diagnostic(config, "decision", decision="skipped", reason="no-results", latency_ms=latency_ms)
        return [], None, [], "no-results"

    results = enhance_results(results, config, cwd=cwd)
    log_recall_candidates(config, results, "enhanced", query=query)

    # CE reranking (only on deep path)
    if top_score < high_conf and config.get("reranker_enabled", True) and len(results) > 1:
        try:
            from tenet_reranker import rerank

            results = rerank(prompt, results, config)
            pipeline_depth += "+ce"
            log_recall_candidates(config, results, "reranked", query=query)
        except Exception:
            pass

    if not results:
        bump_prompts_since()
        log_retrieval("recall", "filtered-empty", query=query, results_before=results_before, latency_ms=latency_ms)
        log_recall_diagnostic(config, "decision", decision="skipped", reason="filtered-empty", latency_ms=latency_ms)
        return [], None, [], "filtered-empty"

    top_path = results[0].get("path", "")
    if is_duplicate(top_path):
        log_retrieval("recall", "dedup-skip", query=query)
        log_recall_diagnostic(config, "decision", decision="skipped", reason="duplicate", top_path=top_path)
        return [], None, [], "duplicate"

    lines = ["[vault] Related memories:"]
    injected = []
    for result in results[:max_notes]:
        lines.append(format_result(result))
        injected.append(result.get("title", ""))

    injected_text = "\n".join(lines)
    log_retrieval(
        "recall",
        "inject",
        query=query,
        latency_ms=latency_ms,
        results_before=results_before,
        results_after=len(results),
        injected_titles=injected,
        injected_chars=len(injected_text),
        pipeline=pipeline_depth,
        multi_hop_gate=multi_hop_gate,
        multi_hop_added=multi_hop_added,
        deep_recall_spawned=deep_recall_spawned,
    )
    log_recall_diagnostic(
        config,
        "decision",
        decision="injected",
        injected_titles=injected,
        injected_chars=len(injected_text),
        latency_ms=latency_ms,
        top_path=top_path,
        pipeline=pipeline_depth,
    )

    return lines, top_path, results[:max_notes], None


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


def build_recall(prompt: str, cwd: str = "", session_id: str = "unknown") -> LifecycleResult:
    """Build prompt recall content."""
    lines, top_path, results, reason = _run_recall_lines(prompt, cwd, session_id)
    if not lines:
        return empty_result("recall", reason or "no-results")
    content = "\n".join(lines)
    if top_path:
        record_recall(top_path)
    return LifecycleResult(
        should_inject=True,
        content=content,
        source="recall",
        results=results,
        metadata={"cwd": cwd, "session_id": session_id, "top_path": top_path},
    )


LAST_RECALL_PATH = os.path.join(RUNTIME_DIR, "last-recall.json")
DEFERRED_BRIEFING_PATH = os.path.join(RUNTIME_DIR, "deferred-briefing.json")
DEEP_RECALL_PENDING_PATH = os.path.join(RUNTIME_DIR, "deep-recall-pending.json")

CACHE_PATH = os.path.join(RUNTIME_DIR, "tool-context-cache.json")
RECALL_STATE_PATH = LAST_RECALL_PATH

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
        if os.path.realpath(file_path).startswith(str(vault)):
            return True
    except (OSError, ValueError):
        pass

    if any(part in SKIP_SEGMENTS for part in parts):
        return True

    if path.suffix.lower() in SKIP_EXTENSIONS:
        return True
    return path.name in SKIP_FILENAMES


def extract_tool_context_keywords(file_path: str) -> str:
    """Extract searchable keywords from a file path for BM25 query."""
    path = file_path
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


def load_cache() -> dict:
    """Load the tool-context cache from disk."""
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH) as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {"dirs": {}, "last_qmd_call": 0, "injections": {}}


def save_cache(cache: dict) -> None:
    """Write the tool-context cache to disk."""
    try:
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass


def get_recall_paths() -> set[str]:
    """Read paths recently injected by prompt recall for dedup."""
    try:
        if os.path.exists(RECALL_STATE_PATH):
            with open(RECALL_STATE_PATH) as f:
                data = json.load(f)
            top = data.get("top_path", "")
            return {top} if top else set()
    except (json.JSONDecodeError, OSError):
        pass
    return set()


def session_injection_count(cache: dict, session_id: str) -> int:
    return cache.get("injections", {}).get(session_id, {}).get("count", 0)


def session_injected_paths(cache: dict, session_id: str) -> set[str]:
    return set(cache.get("injections", {}).get(session_id, {}).get("paths", []))


def record_injection(cache: dict, session_id: str, note_paths: list[str]) -> None:
    if "injections" not in cache:
        cache["injections"] = {}
    if session_id not in cache["injections"]:
        cache["injections"][session_id] = {"count": 0, "paths": []}

    entry = cache["injections"][session_id]
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


def build_tool_context(
    tool_name: str,
    file_path: str,
    cwd: str = "",
    session_id: str = "unknown",
) -> LifecycleResult:
    """Build context for a file-read tool result."""
    config = get_config()
    metadata = {"cwd": cwd, "session_id": session_id, "tool_name": tool_name, "file_path": file_path}

    def no_context(reason: str) -> LifecycleResult:
        return LifecycleResult(False, "", "tool-context", reason=reason, metadata=metadata)

    if not config.get("tool_context", True):
        return no_context("disabled")
    if tool_name not in {"Read", "read"}:
        return no_context("unsupported-tool")
    if not file_path:
        return no_context("missing-file-path")

    try:
        normalized_path = os.path.realpath(os.path.expanduser(file_path))
    except (OSError, ValueError):
        return no_context("invalid-file-path")
    metadata["file_path"] = normalized_path

    if should_skip_tool_context_path(normalized_path):
        return no_context("skipped-path")

    if not has_qmd():
        return no_context("qmd-unavailable")

    cache = load_cache()
    max_injections = config.get("tool_context_max_injections", 5)
    if session_injection_count(cache, session_id) >= max_injections:
        return no_context("cap-reached")

    dir_key = str(Path(normalized_path).parent)
    search_query = None
    latency_ms = 0
    if dir_key in cache.get("dirs", {}):
        cached = cache["dirs"][dir_key]
        results = cached.get("results", [])
        if not results:
            return no_context("no-results")
        log_retrieval("tool-context", "cache-hit", file_path=normalized_path, dir_key=dir_key)
    else:
        cooldown = config.get("tool_context_cooldown", 3)
        last_call = cache.get("last_qmd_call", 0)
        if time.time() - last_call < cooldown:
            return no_context("cooldown")

        search_query = extract_tool_context_keywords(normalized_path)
        metadata["query"] = search_query
        if not search_query or len(search_query.split()) < 2:
            cache.setdefault("dirs", {})[dir_key] = {"results": []}
            save_cache(cache)
            return no_context("insufficient-keywords")

        min_score = config.get("tool_context_min_score", 0.75)
        max_notes = config.get("tool_context_max_notes", 2)
        t0 = time.time()
        results = qmd_search_with_extras(
            search_query,
            limit=max_notes + 5,
            semantic=False,
            timeout=2,
            min_score=min_score,
        )
        latency_ms = int((time.time() - t0) * 1000)
        results = enhance_results(results, config, cwd=cwd)

        cache["last_qmd_call"] = time.time()
        cache.setdefault("dirs", {})[dir_key] = {"results": results}
        save_cache(cache)

        if not results:
            log_retrieval(
                "tool-context",
                "no-results",
                query=search_query,
                file_path=normalized_path,
                latency_ms=latency_ms,
            )
            return no_context("no-results")

    recall_paths = get_recall_paths()
    already_injected = session_injected_paths(cache, session_id)
    exclude = recall_paths | already_injected
    filtered = [r for r in results if r.get("path", "") not in exclude]
    if not filtered:
        return no_context("duplicate")

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
        injected_chars=len(injected_text),
        latency_ms=latency_ms,
    )

    record_injection(cache, session_id, injected_paths)
    save_cache(cache)
    return LifecycleResult(
        should_inject=True,
        content=injected_text,
        source="tool-context",
        results=selected,
        metadata=metadata,
    )
