#!/usr/bin/env python3
"""
Vault briefing — SessionStart hook.
Prints a compact project-aware vault briefing to stdout so Claude
sees relevant notes at the start of every session.
"""

import sys
from pathlib import Path

# Allow imports from the same directory
sys.path.insert(0, str(Path(__file__).parent))

from memento_utils import get_config, get_vault, detect_project, read_hook_input, qmd_search


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
            # Extract wikilink names
            import re
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


def main():
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

    # Read project index for recent sessions and linked notes
    recent_sessions, linked_notes = read_project_index(project_slug)

    # Query QMD for relevant notes
    max_notes = config.get("briefing_max_notes", 5)
    min_score = config.get("briefing_min_score", 0.3)

    # Build search query from project context
    query_parts = [project_slug.replace("-", " ")]
    if git_branch and git_branch not in ("main", "master", "HEAD"):
        branch_words = git_branch.replace("-", " ").replace("/", " ")
        query_parts.append(branch_words)

    qmd_results = qmd_search(
        " ".join(query_parts),
        limit=max_notes,
        semantic=True,
        timeout=12,
        min_score=min_score,
    )

    # Build output
    output_lines = []

    branch_str = f" (branch: {git_branch})" if git_branch else ""
    output_lines.append(f"[vault] Project: {project_slug}{branch_str}")

    if recent_sessions:
        sessions_str = "; ".join(s[:60] for s in recent_sessions[-3:])
        output_lines.append(f"[vault] Last sessions: {sessions_str}")

    # Combine linked notes from project index + QMD results, deduplicated
    seen = set()
    note_lines = []

    # QMD results first (more relevant)
    for result in qmd_results:
        title = result.get("title", "")
        if title in seen:
            continue
        seen.add(title)
        note_lines.append(format_qmd_result(result))

    # Then linked notes from project index (if not already shown)
    for note_name in linked_notes:
        if note_name in seen or len(note_lines) >= max_notes:
            break
        seen.add(note_name)
        oneliner = read_note_oneliner(note_name)
        if oneliner:
            note_lines.append(f"  - {oneliner}")

    if note_lines:
        output_lines.append("[vault] Relevant notes:")
        output_lines.extend(note_lines[:max_notes])

    if len(output_lines) > 1:  # More than just the project line
        print("\n".join(output_lines))


if __name__ == "__main__":
    main()
