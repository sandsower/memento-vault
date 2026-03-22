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

# Allow imports from the same directory
sys.path.insert(0, str(Path(__file__).parent))

from memento_utils import get_config, get_vault, detect_project, has_qmd, read_hook_input

DEFERRED_BRIEFING_PATH = "/tmp/memento-deferred-briefing.json"


def get_git_branch(cwd):
    """Read current git branch from cwd."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True, text=True, timeout=3,
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
        if stripped.startswith("## Sessions") or (stripped.startswith("## ") and stripped[3:4].isalpha() and "-" in stripped):
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

    return sessions[-3:], notes  # last 3 sessions


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


def format_qmd_result(result):
    """Format a QMD search result as a one-liner."""
    title = result.get("title", "")
    score = result.get("score", 0)
    snippet = result.get("snippet", "").strip()

    # Truncate snippet to first sentence or 100 chars
    if snippet:
        dot = snippet.find(".")
        if 0 < dot < 100:
            snippet = snippet[:dot + 1]
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
    from memento_utils import qmd_search

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

        results = qmd_search(
            query,
            limit=max_notes,
            semantic=True,
            timeout=12,
            min_score=min_score,
        )

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

        with open(DEFERRED_BRIEFING_PATH, "w") as f:
            json.dump({
                "status": "ready",
                "note_lines": note_lines[:max_notes],
                "timestamp": time.time(),
            }, f)

    except Exception:
        # Clean up on failure
        try:
            os.unlink(DEFERRED_BRIEFING_PATH)
        except OSError:
            pass


def main():
    # Handle background worker mode
    if "--deferred" in sys.argv:
        run_deferred_search()
        sys.exit(0)

    config = get_config()

    if not config.get("session_briefing", True):
        sys.exit(0)

    vault = get_vault()
    if not vault.exists() or not (vault / "notes").exists():
        sys.exit(0)

    try:
        hook_input = read_hook_input()
    except Exception:
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

    output_lines = []

    branch_str = f" (branch: {git_branch})" if git_branch else ""
    output_lines.append(f"[vault] Project: {project_slug}{branch_str}")

    if recent_sessions:
        output_lines.append("[vault] Last sessions:")
        for s in recent_sessions[-3:]:
            abbreviated = re.sub(
                r'`([0-9a-f]{8})[0-9a-f-]{28}`',
                r'`\1`',
                s,
            )
            output_lines.append(f"  - {abbreviated[:120]}")

    if len(output_lines) > 1:
        print("\n".join(output_lines))

    # --- Async: spawn background QMD search ---

    if has_qmd():
        spawn_deferred_search(project_slug, git_branch, linked_notes, config)


if __name__ == "__main__":
    main()
