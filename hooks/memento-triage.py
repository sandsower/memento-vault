#!/usr/bin/env python3
"""
Memento triage — runs on SessionEnd.
Reads hook input from stdin, parses the transcript, scores the session,
writes a fleeting note or spawns the memento agent.
"""

import json
import sys
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Shared utilities
_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(Path(__file__).parent))
from memento.config import detect_project, get_config, get_vault  # noqa: E402
from memento.llm import llm_complete  # noqa: E402
from memento.store import (  # noqa: E402
    acquire_vault_write_lock,
    load_inception_state,
    log_retrieval,
    release_vault_write_lock,
    update_project_index,
    write_note,
)
from memento.adapters import parse_transcript  # noqa: E402
from memento.utils import normalize_note_tags, read_hook_input, sanitize_secrets  # noqa: E402


# --- Substantiality scoring ---


# Keywords that signal a high-value session even with few exchanges
_INSIGHT_KEYWORDS = re.compile(
    r"\b(bug|fix|broke|error|issue|debug|crash|regression|root cause|why does|how to)\b",
    re.IGNORECASE,
)


def is_substantial(meta):
    """Score whether a session is substantial or trivial."""
    config = get_config()

    if meta["exchange_count"] > config["exchange_threshold"]:
        return True
    if len(meta["files_edited"]) > config["file_count_threshold"]:
        return True

    notable_patterns = config["notable_patterns"]
    for f in meta["files_edited"]:
        for pattern in notable_patterns:
            if pattern in f:
                return True

    # Short but meaty: keyword match in first prompt + at least 5 exchanges
    if meta["exchange_count"] >= 5 and meta.get("first_prompt"):
        if _INSIGHT_KEYWORDS.search(meta["first_prompt"]):
            return True

    # Read-heavy investigation sessions (deep dives)
    if len(meta.get("files_read", [])) >= 6:
        return True

    return False


def has_new_insight(meta):
    """Delta-check: query QMD for existing coverage of this session's topics.
    Returns True if the session likely contains new information not already
    in the vault. Falls back to True if QMD is unavailable."""
    import shutil

    if not shutil.which("qmd"):
        return True

    config = get_config()

    # Build a search query from the session's key signals
    query_parts = []
    if meta["first_prompt"]:
        query_parts.append(meta["first_prompt"][:120])
    if meta["git_branch"] and meta["git_branch"] != "HEAD":
        branch_words = re.sub(r"[^a-z0-9]", " ", meta["git_branch"].lower()).strip()
        if branch_words:
            query_parts.append(branch_words)

    if not query_parts:
        return True

    query = " ".join(query_parts)

    try:
        result = subprocess.run(
            ["qmd", "search", query, "-c", config["qmd_collection"], "-n", "5"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return True

        lines = result.stdout.strip().splitlines()
        hit_count = sum(1 for line in lines if line.strip() and not line.startswith("#"))

        # Also check extra collections for broader coverage
        for extra in config.get("extra_qmd_collections", []):
            try:
                extra_result = subprocess.run(
                    ["qmd", "search", query, "-c", extra, "-n", "3"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if extra_result.returncode == 0:
                    extra_lines = extra_result.stdout.strip().splitlines()
                    hit_count += sum(1 for line in extra_lines if line.strip() and not line.startswith("#"))
            except (subprocess.TimeoutExpired, OSError):
                pass

        if hit_count < 3:
            return True

        # Even with good coverage, new files mean new work
        if len(meta["files_edited"]) > 0:
            vault_path = get_config()["vault_path"]
            non_vault = [f for f in meta["files_edited"] if vault_path not in f]
            if non_vault:
                return True

        return False

    except subprocess.TimeoutExpired:
        return True
    except Exception as exc:
        log_retrieval("triage", "has_new_insight_failed", error=str(exc))
        return True


# --- Helpers ---


def ensure_project_index(project_slug, cwd, git_branch):
    """Create or return path to project index file."""
    project_file = get_vault() / "projects" / f"{project_slug}.md"
    if not project_file.exists():
        lines = [
            "---",
            f"title: {project_slug}",
            f"project: {cwd or 'unknown'}",
        ]
        if git_branch and git_branch != "HEAD":
            lines.append(f"branch: {git_branch}")
        lines.extend(["---", "", "## Notes", "", "## Sessions", ""])
        project_file.write_text("\n".join(lines))
    return project_file


def append_session_to_project(project_file, session_id, summary, ticket=None):
    """Append a session line to the project index."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    line = f"- {today} `{session_id}` — {summary}\n"
    content = project_file.read_text()

    if ticket:
        ticket_header = f"## {ticket}:"
        if ticket_header in content:
            idx = content.index(ticket_header)
            next_section = content.find("\n## ", idx + len(ticket_header))
            if next_section == -1:
                content = content.rstrip("\n") + "\n" + line
            else:
                content = content[:next_section].rstrip("\n") + "\n" + line + "\n" + content[next_section:]
        else:
            content = content.rstrip("\n") + f"\n\n## {ticket}\n\n" + line
    else:
        if "## Sessions" in content:
            content = content.rstrip("\n") + "\n" + line
        else:
            content += f"\n## Sessions\n{line}"

    project_file.write_text(content)


def write_fleeting(session_id, meta, project_slug):
    """Write a one-liner to today's fleeting note."""
    vault = get_vault()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).strftime("%H:%M")
    fleeting_file = vault / "fleeting" / f"{today}.md"

    if not fleeting_file.exists():
        fleeting_file.write_text(f"# {today}\n\n")

    branch_str = ""
    if meta["git_branch"] and meta["git_branch"] != "HEAD":
        branch_str = f" ({meta['git_branch']})"

    files_str = ""
    if meta["files_edited"]:
        files_str = f", {len(meta['files_edited'])} files edited"

    prompt_str = ""
    if meta["first_prompt"]:
        prompt_str = meta["first_prompt"][:100]
        if len(meta["first_prompt"]) > 100:
            prompt_str += "..."

    line = f"- {now} `{session_id}` {meta['cwd'] or '?'}{branch_str} — {meta['exchange_count']} exchanges{files_str}"
    if prompt_str:
        line += f" — {prompt_str}"
    if meta.get("last_outcome"):
        line += f" → {meta['last_outcome']}"
    line += "\n"

    with open(fleeting_file, "a") as f:
        f.write(line)


def build_session_summary(meta):
    """Build a short summary string for the project index."""
    parts = []
    if meta["first_prompt"]:
        parts.append(meta["first_prompt"][:80])
    else:
        parts.append(f"{meta['exchange_count']} exchanges")
    if meta["files_edited"]:
        parts.append(f"{len(meta['files_edited'])} files")
    return ", ".join(parts)


# --- Vault operations ---


VAULT_COMMIT = Path(__file__).parent / "vault-commit.sh"


def normalize_all_notes():
    """Normalize tags on all notes in the vault. Safe to call multiple times."""
    vault = get_vault()
    notes_dir = vault / "notes"
    if not notes_dir.exists():
        return
    for note_path in notes_dir.glob("*.md"):
        normalize_note_tags(note_path)


def vault_commit(message="auto: vault update", delay_seconds=0, sentinel=None):
    """Commit all vault changes. Runs detached.

    If sentinel is provided, waits for that file to appear (agent completion)
    before normalizing tags and committing. Falls back to delay_seconds as a
    hard timeout if the sentinel never appears.
    """
    commit_script = str(VAULT_COMMIT)
    if not VAULT_COMMIT.exists():
        # Fall back to looking in the install location
        commit_script = str(Path.home() / ".claude" / "hooks" / "vault-commit.sh")

    if sentinel:
        # Wait for agent completion, then normalize and commit
        hooks_dir = str(Path(__file__).parent)
        max_wait = max(delay_seconds, 120)
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).parent / "wait-and-commit.py"),
                str(sentinel),
                str(max_wait),
                hooks_dir,
                commit_script,
                message,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    else:
        # Pass arguments via sys.argv to avoid shell injection from message/path content
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess,time,sys; time.sleep(int(sys.argv[1])); subprocess.run([sys.argv[2], sys.argv[3]], capture_output=True)",
                str(delay_seconds),
                commit_script,
                message,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def reindex_qmd(delay_seconds=0):
    """Reindex the memento collection in QMD. Runs detached. No-op if QMD is not installed."""
    import shutil

    if not shutil.which("qmd"):
        return

    config = get_config()
    collection = config["qmd_collection"]

    # Pass collection name via sys.argv to avoid injection
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,time,shutil,sys; time.sleep(int(sys.argv[1])); qmd=shutil.which('qmd');"
            " qmd and subprocess.run([qmd,'update','-c',sys.argv[2]], capture_output=True);"
            " qmd and subprocess.run([qmd,'embed'], capture_output=True)",
            str(delay_seconds),
            collection,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _sentinel_path(session_id):
    """Return the sentinel path for a session's note-writing run."""
    vault = get_vault()
    return vault / ".agent-done" / f"{session_id[:8]}.done"


def _parse_structured_notes_response(raw):
    """Parse structured note output from the LLM into a list of note dicts."""
    if not raw:
        return []

    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        stripped = "\n".join(lines)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return []

    if isinstance(data, dict):
        data = data.get("notes", [])

    if not isinstance(data, list):
        return []

    return [item for item in data if isinstance(item, dict) and item.get("title") and item.get("body")]


def process_structured_notes(session_id, transcript_path, meta, project_slug):
    """Read transcript, call the shared LLM, and write structured notes."""
    vault = get_vault()
    try:
        transcript_text = sanitize_secrets(Path(transcript_path).read_text())
    except OSError:
        log_retrieval(
            "triage",
            "structured_notes_transcript_unreadable",
            session_id=session_id,
            project=project_slug,
        )
        return 0

    existing_titles = []
    notes_dir = vault / "notes"
    if notes_dir.exists():
        for note_path in notes_dir.glob("*.md"):
            existing_titles.append(note_path.stem)

    prompt = (
        "Read this session transcript and return JSON only.\n"
        'Return either a JSON array of notes or {"notes": [...]}.\n'
        "Each note must include: title, body, type, tags, certainty.\n"
        "Optional fields: validity_context, supersedes.\n"
        "Do not include any prose outside JSON.\n\n"
        f"Session ID: {session_id}\n"
        f"Project slug: {project_slug}\n"
        f"CWD: {meta.get('cwd')}\n"
        f"Branch: {meta.get('git_branch')}\n"
        f"Edited files: {json.dumps(meta.get('files_edited', []))}\n"
        f"Existing notes: {json.dumps(existing_titles[:100])}\n\n"
        "Transcript:\n"
        f"{transcript_text}"
    )

    result = llm_complete(prompt)
    if not result.ok:
        log_retrieval(
            "triage",
            "structured_notes_llm_failed",
            session_id=session_id,
            project=project_slug,
            error=result.error or "unknown llm error",
        )
        return 0

    notes = _parse_structured_notes_response(result.text)
    if not notes:
        log_retrieval(
            "triage",
            "structured_notes_parse_empty",
            session_id=session_id,
            project=project_slug,
            raw_preview=result.text[:200] if result.text else "",
        )
        return 0

    summary = build_session_summary(meta)
    if not acquire_vault_write_lock():
        log_retrieval(
            "triage",
            "structured_notes_lock_timeout",
            session_id=session_id,
            project=project_slug,
        )
        return 0
    try:
        written = 0
        for note in notes:
            path = write_note(
                vault,
                title=note["title"],
                body=sanitize_secrets(note["body"]),
                note_type=note.get("type", "discovery"),
                tags=note.get("tags", []),
                certainty=note.get("certainty"),
                validity_context=note.get("validity_context") or note.get("validity-context"),
                supersedes=note.get("supersedes"),
                project=meta.get("cwd"),
                branch=meta.get("git_branch"),
                session_id=session_id,
            )
            update_project_index(vault, project_slug, path.stem, f"`{session_id}` — {summary}")
            written += 1
        return written
    finally:
        release_vault_write_lock()


def _run_structured_notes_worker(payload_path, sentinel_path):
    """Detached worker for structured note extraction."""
    try:
        with open(payload_path) as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        payload = None
    finally:
        try:
            os.unlink(payload_path)
        except OSError:
            pass

    try:
        if payload:
            try:
                written = process_structured_notes(
                    payload["session_id"],
                    payload["transcript_path"],
                    payload["meta"],
                    payload["project_slug"],
                )
                if written == 0:
                    log_retrieval(
                        "triage",
                        "structured_notes_empty",
                        session_id=payload["session_id"],
                        project=payload["project_slug"],
                    )
            except Exception as exc:
                log_retrieval(
                    "triage",
                    "structured_notes_failed",
                    session_id=payload["session_id"],
                    error=str(exc),
                    project=payload["project_slug"],
                )
    finally:
        sentinel = Path(sentinel_path)
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()


def spawn_memento_agent(session_id, transcript_path, meta, project_slug):
    """Spawn a background structured-note worker.

    Returns the sentinel Path that is touched when the process finishes.
    """
    vault = get_vault()

    sentinel = _sentinel_path(session_id)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.unlink(missing_ok=True)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="triage-notes-", dir=vault, delete=False) as tmp:
        json.dump(
            {
                "session_id": session_id,
                "transcript_path": transcript_path,
                "meta": meta,
                "project_slug": project_slug,
            },
            tmp,
        )
        payload_path = tmp.name

    subprocess.Popen(
        [sys.executable, __file__, "--structured-notes", payload_path, str(sentinel)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return sentinel


# --- Main ---


def run_remote_triage(hook_input):
    """Run triage via the remote vault client — sends capture request over HTTP.

    Mirrors local triage semantics:
    - All sessions with >=2 exchanges get a fleeting entry + project index update
    - Only substantial sessions also get a permanent atomic note

    The server-side memento_capture supports fleeting_only=True to write only
    the fleeting log without creating a permanent note.
    """
    from memento.remote_client import capture as remote_capture

    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path")

    if not transcript_path or not os.path.exists(transcript_path):
        return

    try:
        meta = parse_transcript(transcript_path)
    except Exception:
        return

    if meta["exchange_count"] < 2:
        return

    if not meta["cwd"]:
        meta["cwd"] = hook_input.get("cwd")

    substantial = is_substantial(meta)
    new_insight = has_new_insight(meta) if substantial else False
    summary = build_session_summary(meta)
    result = remote_capture(
        session_summary=summary,
        cwd=meta.get("cwd", ""),
        branch=meta.get("git_branch", ""),
        files_edited=meta.get("files_edited", []),
        session_id=session_id,
        agent="claude",
        fleeting_only=not (substantial and new_insight),
    )

    if isinstance(result, dict) and "error" in result:
        print(f"[memento] remote capture failed for session {session_id}: {result['error']}", file=sys.stderr)
        # Spool to an isolated directory so data isn't lost but doesn't pollute
        # the main vault. Operator can reconcile later via the spool dir.
        try:
            vault = get_vault()
            spool_dir = vault / "spool" / "remote-failures"
            spool_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)[:64]
            spool_file = spool_dir / f"{ts}-{safe_id}.md"
            sanitized = sanitize_secrets(summary)
            fm = {
                "session_id": session_id,
                "branch": str(meta.get("git_branch", "")),
                "cwd": str(meta.get("cwd", "")),
                "error": str(result["error"]),
                "captured": ts,
            }
            fm_lines = "\n".join(f"{k}: {json.dumps(v)}" for k, v in fm.items())
            spool_file.write_text(
                f"---\n{fm_lines}\n---\n\n{sanitized}\n"
            )
            print(f"[memento] spooled session to {spool_file} for later reconciliation", file=sys.stderr)
        except Exception as fallback_exc:
            print(f"[memento] spool fallback also failed: {fallback_exc}", file=sys.stderr)


def main():
    try:
        hook_input = read_hook_input()
    except Exception as exc:
        log_retrieval("triage", "hook_input_failed", error=str(exc))
        sys.exit(0)

    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path")

    if not transcript_path or not os.path.exists(transcript_path):
        sys.exit(0)

    try:
        meta = parse_transcript(transcript_path)
    except Exception as exc:
        log_retrieval("triage", "parse_transcript_failed", error=str(exc), session_id=session_id)
        sys.exit(0)

    if not meta["cwd"]:
        meta["cwd"] = hook_input.get("cwd")

    if meta["exchange_count"] < 2:
        sys.exit(0)

    config = get_config()
    vault = get_vault()

    # Ensure vault directories exist
    (vault / "fleeting").mkdir(parents=True, exist_ok=True)
    (vault / "notes").mkdir(parents=True, exist_ok=True)
    (vault / "projects").mkdir(parents=True, exist_ok=True)
    (vault / "archive").mkdir(parents=True, exist_ok=True)

    project_slug, ticket = detect_project(meta["cwd"], meta["git_branch"])

    write_fleeting(session_id, meta, project_slug)

    project_file = ensure_project_index(project_slug, meta["cwd"], meta["git_branch"])
    summary = build_session_summary(meta)
    append_session_to_project(project_file, session_id, summary, ticket=ticket)

    if config["auto_commit"]:
        vault_commit(f"auto: triage session {session_id[:8]}")

    substantial = is_substantial(meta)
    new_insight = has_new_insight(meta) if substantial else False

    log_retrieval(
        "triage",
        "decision",
        session_id=session_id[:8],
        project=project_slug,
        exchanges=meta["exchange_count"],
        files_edited=len(meta["files_edited"]),
        substantial=substantial,
        new_insight=new_insight,
        agent_spawned=substantial and new_insight,
    )

    if substantial and new_insight:
        sentinel = spawn_memento_agent(session_id, transcript_path, meta, project_slug)
        delay = config["agent_delay_seconds"]
        # Backfill certainty on any notes the agent missed
        backfill_certainty(delay_seconds=delay - 5)
        if config["auto_commit"]:
            vault_commit(f"auto: notes from session {session_id[:8]}", delay_seconds=delay, sentinel=sentinel)
        reindex_qmd(delay_seconds=delay + 5)
    else:
        # Always reindex so fleeting notes become searchable
        reindex_qmd()

    # Inception: background consolidation (gated)
    if config.get("inception_enabled", False):
        maybe_trigger_inception(config)

    # Additionally sync to remote vault if configured
    from memento.remote_client import is_remote

    if is_remote():
        try:
            run_remote_triage(hook_input)
        except Exception as exc:
            print(f"[memento] remote sync failed (local capture succeeded): {exc}", file=sys.stderr)

    sys.exit(0)


def backfill_certainty(delay_seconds=0):
    """Scan vault notes for missing certainty and backfill from type/source.

    Runs detached after a delay so the memento agent has time to write first.
    """
    vault = get_vault()
    backfill_script = str(Path(__file__).parent / "memento-sweeper.py")

    # Use the sweeper's backfill subcommand if available, otherwise inline
    if Path(backfill_script).exists():
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time,subprocess,sys; time.sleep(int(sys.argv[1])); "
                "subprocess.run([sys.argv[2], sys.argv[3], 'backfill-certainty', sys.argv[4]], capture_output=True)",
                str(max(delay_seconds, 0)),
                sys.executable,
                backfill_script,
                str(vault / "notes"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return

    # Inline fallback: scan notes and patch missing certainty
    notes_dir = str(vault / "notes")
    subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "_backfill_certainty.py"), notes_dir, str(max(delay_seconds, 0))],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def maybe_trigger_inception(config):
    """Spawn the Inception if enough new notes have accumulated."""
    state = load_inception_state()
    vault = Path(config["vault_path"])
    notes_dir = vault / "notes"

    if not notes_dir.exists():
        return

    last_run = state.get("last_run_iso")
    threshold = config.get("inception_threshold", 5)

    if last_run:
        try:
            cutoff = datetime.fromisoformat(last_run)
            new_count = sum(1 for f in notes_dir.glob("*.md") if datetime.fromtimestamp(f.stat().st_mtime) > cutoff)
        except (ValueError, OSError):
            new_count = 0
    else:
        new_count = len(list(notes_dir.glob("*.md")))

    if new_count < threshold:
        log_retrieval("inception", "skip", new_notes=new_count, threshold=threshold)
        return

    inception_script = Path(__file__).parent / "memento-inception.py"
    if not inception_script.exists():
        inception_script = Path.home() / ".claude" / "hooks" / "memento-inception.py"

    if not inception_script.exists():
        return

    log_retrieval("inception", "trigger", new_notes=new_count, threshold=threshold, last_run=last_run)

    subprocess.Popen(
        [sys.executable, str(inception_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--structured-notes":
        _run_structured_notes_worker(sys.argv[2], sys.argv[3])
    else:
        main()
