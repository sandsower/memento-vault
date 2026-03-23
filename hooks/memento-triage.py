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
from datetime import datetime, timezone
from pathlib import Path

# Shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from memento_utils import get_config, get_vault, detect_project, slugify, read_hook_input, sanitize_secrets, log_retrieval


# --- Transcript parsing ---


def parse_transcript(transcript_path):
    """Parse a Claude transcript JSONL file. Returns metadata dict."""
    user_count = 0
    assistant_count = 0
    files_edited = set()
    git_branch = None
    cwd = None
    first_user_prompt = None

    with open(transcript_path) as f:
        for line in f:
            entry = json.loads(line)
            msg_type = entry.get("type")
            cwd = cwd or entry.get("cwd")
            git_branch = git_branch or entry.get("gitBranch")

            if msg_type == "user":
                user_count += 1
                msg = entry.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str) and not first_user_prompt:
                    # Strip system tags from prompt text
                    cleaned = re.sub(r"<[^>]+>.*?</[^>]+>", "", content, flags=re.DOTALL).strip()
                    if cleaned:
                        first_user_prompt = sanitize_secrets(cleaned[:200])

            elif msg_type == "assistant":
                assistant_count += 1
                msg = entry.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name = block.get("name", "")
                            inp = block.get("input", {})
                            if name in ("Edit", "Write"):
                                fp = inp.get("file_path", "")
                                if fp:
                                    files_edited.add(fp)

    exchange_count = min(user_count, assistant_count)
    return {
        "cwd": cwd,
        "git_branch": git_branch,
        "exchange_count": exchange_count,
        "user_messages": user_count,
        "files_edited": sorted(files_edited),
        "first_prompt": first_user_prompt,
    }


# --- Substantiality scoring ---


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
            capture_output=True, text=True, timeout=10,
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
                    capture_output=True, text=True, timeout=10,
                )
                if extra_result.returncode == 0:
                    extra_lines = extra_result.stdout.strip().splitlines()
                    hit_count += sum(1 for line in extra_lines if line.strip() and not line.startswith("#"))
            except Exception:
                pass

        if hit_count < 3:
            return True

        # Even with good coverage, new files mean new work
        if len(meta["files_edited"]) > 0:
            vault_path = get_config()["vault_path"]
            non_vault = [f for f in meta["files_edited"]
                         if vault_path not in f]
            if non_vault:
                return True

        return False

    except (subprocess.TimeoutExpired, Exception):
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

    line = (
        f"- {now} `{session_id}` {meta['cwd'] or '?'}{branch_str}"
        f" — {meta['exchange_count']} exchanges{files_str}"
    )
    if prompt_str:
        line += f" — {prompt_str}"
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


def vault_commit(message="auto: vault update", delay_seconds=0):
    """Commit all vault changes. Runs detached. Optional delay for agent writes."""
    commit_script = str(VAULT_COMMIT)
    if not VAULT_COMMIT.exists():
        # Fall back to looking in the install location
        commit_script = str(Path.home() / ".claude" / "hooks" / "vault-commit.sh")

    # Pass arguments via sys.argv to avoid shell injection from message/path content
    subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,time,sys; time.sleep(int(sys.argv[1])); subprocess.run([sys.argv[2], sys.argv[3]], capture_output=True)",
         str(delay_seconds), commit_script, message],
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
        [sys.executable, "-c",
         "import subprocess,time,shutil,sys; time.sleep(int(sys.argv[1])); qmd=shutil.which('qmd');"
         " qmd and subprocess.run([qmd,'update','-c',sys.argv[2]], capture_output=True);"
         " qmd and subprocess.run([qmd,'embed'], capture_output=True)",
         str(delay_seconds), collection],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def spawn_memento_agent(session_id, transcript_path, meta, project_slug):
    """Spawn a background Claude call to generate atomic notes."""
    config = get_config()
    vault = get_vault()

    prompt = f"""You are the Memento agent. Read the transcript and create atomic Zettelkasten notes.

Session ID: {session_id}
Project: {meta['cwd']}
Branch: {meta.get('git_branch', 'unknown')}
Files edited: {json.dumps(meta['files_edited'])}

Transcript path: {transcript_path}

Instructions:
1. Read the transcript at the path above
2. Identify distinct decisions, discoveries, patterns, bugfixes, or tools from this session
3. For each one, create an atomic note in {vault}/notes/
4. Each note must have YAML frontmatter with: title, type (decision|discovery|pattern|bugfix|tool), tags, source: session, certainty (1-5), validity-context (optional), supersedes (optional wikilink), project (full cwd path), branch, date (ISO 8601 with time: YYYY-MM-DDTHH:MM), session_id
   Certainty scale: 1=speculative, 2=observed once, 3=confirmed in code, 4=tested/shipped, 5=established pattern
   validity-context: short phrase for what this note depends on. Omit if unconditionally true.
   supersedes: if this note replaces an older one, add [[older-note-name]].
5. Name files as slugified concept titles (e.g., redis-cache-requires-explicit-ttl.md)
6. DEDUP CHECK: Before writing each note, search {vault}/notes/ for existing notes on the same topic.
   Read the top 2-3 matches by filename/title similarity. Then decide:
   - If an existing note covers the same ground with equal or higher certainty: SKIP this note entirely.
   - If an existing note covers the same topic but this session has new evidence (higher certainty,
     additional details, a correction): write the new note with supersedes: "[[existing-note-name]]"
     in frontmatter. Set certainty >= the old note's certainty. Add a line in the body:
     "Supersedes [[old-note]] — [what changed]."
   - If no existing note matches: write normally.
7. Search existing notes in {vault}/notes/ for related topics and add [[wikilinks]]
8. Add a Related section at the bottom of each note linking to related existing notes
9. Update the project index at {vault}/projects/{project_slug}.md — add [[note-name]] links under the Notes section
10. Never overwrite or delete existing notes
11. Never write to fleeting/
12. SECURITY: Never include secrets, API keys, tokens, passwords, or connection strings in notes.
    Redact any sensitive values you find in the transcript. Replace them with [REDACTED].
    Common patterns: sk-*, ghp_*, xoxb-*, AKIA*, Bearer tokens, postgres:// URLs, JWT tokens.
"""

    cmd = [
        "claude",
        "--print",
        "--model", config["agent_model"],
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--allowedTools", "Read", "Write", "Edit", "Glob", "Grep",
        "--add-dir", str(vault),
        "-p", prompt,
    ]

    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# --- Main ---


def main():
    try:
        hook_input = read_hook_input()
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path")

    if not transcript_path or not os.path.exists(transcript_path):
        sys.exit(0)

    try:
        meta = parse_transcript(transcript_path)
    except Exception:
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

    log_retrieval("triage", "decision",
                  session_id=session_id[:8], project=project_slug,
                  exchanges=meta["exchange_count"],
                  files_edited=len(meta["files_edited"]),
                  substantial=substantial, new_insight=new_insight,
                  agent_spawned=substantial and new_insight)

    if substantial and new_insight:
        spawn_memento_agent(session_id, transcript_path, meta, project_slug)
        delay = config["agent_delay_seconds"]
        if config["auto_commit"]:
            vault_commit(f"auto: notes from session {session_id[:8]}", delay_seconds=delay)
        reindex_qmd(delay_seconds=delay + 5)
    else:
        reindex_qmd()

    # Inception: background consolidation (gated)
    if config.get("inception_enabled", False):
        maybe_trigger_inception(config)

    sys.exit(0)


def maybe_trigger_inception(config):
    """Spawn the Inception if enough new notes have accumulated."""
    from memento_utils import load_inception_state

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
            new_count = sum(
                1 for f in notes_dir.glob("*.md")
                if datetime.fromtimestamp(f.stat().st_mtime) > cutoff
            )
        except (ValueError, OSError):
            new_count = 0
    else:
        new_count = len(list(notes_dir.glob("*.md")))

    if new_count < threshold:
        log_retrieval("inception", "skip",
                      new_notes=new_count, threshold=threshold)
        return

    inception_script = Path(__file__).parent / "memento-inception.py"
    if not inception_script.exists():
        inception_script = Path.home() / ".claude" / "hooks" / "memento-inception.py"

    if not inception_script.exists():
        return

    log_retrieval("inception", "trigger",
                  new_notes=new_count, threshold=threshold,
                  last_run=last_run)

    subprocess.Popen(
        [sys.executable, str(inception_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


if __name__ == "__main__":
    main()
