#!/usr/bin/env python3
"""
Vault briefing — SessionStart hook.
Prints a compact project-aware vault briefing to stdout so Claude
sees relevant notes at the start of every session.

Fast path: project line + recent sessions from index (file I/O only, <50ms).
QMD search is deferred to a background subprocess — results are picked up
by vault-recall.py on the first UserPromptSubmit.
"""

import json
import os
import re
import subprocess as _subprocess
import sys
import time
from pathlib import Path

# Allow imports from the repo and same directory
_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(Path(__file__).parent))

from memento.config import RUNTIME_DIR, detect_project, get_config, get_vault  # noqa: E402
from memento.graph import load_or_build_graph, lookup_project_notes  # noqa: E402
from memento.search import enhance_results, has_qmd, qmd_search  # noqa: E402
from memento.store import log_retrieval  # noqa: E402
from memento.utils import read_hook_input  # noqa: E402

DEFERRED_BRIEFING_PATH = os.path.join(RUNTIME_DIR, "deferred-briefing.json")


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
            [sys.executable, __file__, "--deferred"],
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


def run_deferred_search():
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
    """Run briefing via the remote vault client."""
    from memento.remote_client import status as remote_status, search as remote_search

    vault_status = remote_status()
    if not vault_status or "error" in vault_status:
        return

    note_count = vault_status.get("note_count", 0)

    # Derive project from cwd
    git_branch = get_git_branch(cwd)
    from memento.config import detect_project

    project_slug, ticket = detect_project(cwd, git_branch)

    if project_slug == "unknown":
        return

    branch_str = f" ({git_branch})" if git_branch else ""
    summary = f"[vault] Project: {project_slug}{branch_str} | {note_count} notes (remote)"
    print(summary)

    # Search for project-relevant notes
    max_notes = config.get("briefing_max_notes", 5)
    query = project_slug.replace("-", " ")
    if git_branch and git_branch not in ("main", "master", "HEAD"):
        query += " " + git_branch.replace("-", " ").replace("/", " ")

    results = remote_search(query=query, limit=max_notes, cwd=cwd)
    if results:
        note_lines = []
        for r in results[:max_notes]:
            title = r.get("title", "")
            note_lines.append(f"  - {title}")

        # Write as ready for vault-recall to pick up
        with open(DEFERRED_BRIEFING_PATH, "w") as f:
            json.dump({"status": "ready", "note_lines": note_lines, "timestamp": time.time(), "source": "remote"}, f)


def main():
    # Handle background worker mode
    if "--deferred" in sys.argv:
        run_deferred_search()
        sys.exit(0)

    config = get_config()

    if not config.get("session_briefing", True):
        sys.exit(0)

    try:
        hook_input = read_hook_input()
    except Exception as exc:
        log_retrieval("briefing", "hook_input_failed", error=str(exc))
        sys.exit(0)

    # Try remote vault first (has cross-device data), fall through to local
    from memento.remote_client import is_remote

    if is_remote():
        try:
            # Clear any stale deferred briefing from a prior session
            if os.path.exists(DEFERRED_BRIEFING_PATH):
                os.unlink(DEFERRED_BRIEFING_PATH)

            cwd = hook_input.get("cwd", "")
            if cwd:
                run_remote_briefing(cwd, config)
                # Check if remote produced results (file was just written by run_remote_briefing)
                if os.path.exists(DEFERRED_BRIEFING_PATH):
                    import json as _json
                    with open(DEFERRED_BRIEFING_PATH) as _f:
                        _data = _json.load(_f)
                    if _data.get("note_lines"):
                        sys.exit(0)  # remote had results, done
        except Exception as exc:
            print(f"[memento] remote vault unreachable, using local only ({exc})", file=sys.stderr)

    vault = get_vault()
    if not vault.exists() or not (vault / "notes").exists():
        sys.exit(0)

    cwd = hook_input.get("cwd", "")
    if not cwd:
        sys.exit(0)

    git_branch = get_git_branch(cwd)
    project_slug, ticket = detect_project(cwd, git_branch)

    if project_slug == "unknown":
        sys.exit(0)

    # --- Sync: fast project + sessions output (file I/O only) ---

    recent_sessions, linked_notes = read_project_index(project_slug)

    # Count notes in vault for context
    notes_dir = vault / "notes"
    note_count = len(list(notes_dir.glob("*.md"))) if notes_dir.exists() else 0

    # Compact single-line summary (saves ~320 chars vs old 3-line session history)
    branch_str = f" ({git_branch})" if git_branch else ""
    last_date = ""
    if recent_sessions:
        # Extract date from most recent session line (format: "YYYY-MM-DD ...")
        last_line = recent_sessions[-1]
        date_match = re.match(r"(\d{4}-\d{2}-\d{2})", last_line)
        if date_match:
            last_date = f", last: {date_match.group(1)}"

    summary = f"[vault] Project: {project_slug}{branch_str}"
    summary += f" | {len(recent_sessions)} sessions{last_date} | {note_count} notes"
    print(summary)

    # --- Fast path: project maps (skip deferred vsearch if maps have enough results) ---
    if config.get("project_maps_enabled", True) and has_qmd():
        try:
            max_notes = config.get("briefing_max_notes", 5)
            map_notes = lookup_project_notes(project_slug, limit=max_notes)
            if len(map_notes) >= max_notes:
                # Project maps have enough context — format and write as ready
                note_lines = []
                for note in map_notes[:max_notes]:
                    title = note.get("title", "")
                    note_lines.append(f"  - {title}")

                # Write directly as ready (skip deferred vsearch)
                import json as _json

                with open(DEFERRED_BRIEFING_PATH, "w") as f:
                    _json.dump(
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
                return  # Skip deferred vsearch
        except Exception:
            pass  # Fall through to deferred vsearch

    # --- Pre-build wikilink graph for recall (non-blocking) ---
    try:
        load_or_build_graph(get_vault())
    except Exception:
        pass  # Non-fatal — graph will be built on first recall if needed

    # --- Async: spawn background QMD search ---

    if has_qmd():
        config["_cwd"] = cwd
        spawn_deferred_search(project_slug, git_branch, linked_notes, config)


if __name__ == "__main__":
    main()
