"""State, logging, note writing, and vault write coordination."""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from memento.config import RUNTIME_DIR, get_config, slugify

RETRIEVAL_LOG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
    "memento-vault",
    "retrieval.jsonl",
)

INCEPTION_STATE_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
    "memento-vault",
    "inception-state.json",
)

INCEPTION_LOCK_PATH = os.path.join(RUNTIME_DIR, "inception.lock")
VAULT_WRITE_LOCK_PATH = os.path.join(RUNTIME_DIR, "vault-write.lock")


def _should_log():
    """Check if retrieval logging is enabled (config or env var)."""
    if os.environ.get("MEMENTO_DEBUG"):
        return True
    return get_config().get("retrieval_log", False)


def log_retrieval(hook, action, **kwargs):
    """Append a structured log entry to the retrieval log."""
    if not _should_log():
        return

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "hook": hook,
        "action": action,
    }
    entry.update(kwargs)

    try:
        log_dir = os.path.dirname(RETRIEVAL_LOG_PATH)
        os.makedirs(log_dir, exist_ok=True)
        with open(RETRIEVAL_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


def load_inception_state(state_path=None):
    """Load Inception state from disk. Returns defaults if missing/corrupt."""
    path = state_path or INCEPTION_STATE_PATH
    defaults = {
        "last_run_iso": None,
        "last_run_note_count": 0,
        "runs": [],
        "processed_notes": [],
    }
    try:
        with open(path) as f:
            state = json.load(f)
        for key, value in defaults.items():
            state.setdefault(key, value)
        return state
    except FileNotFoundError:
        return dict(defaults)
    except (json.JSONDecodeError, KeyError):
        bak = path + ".bak"
        try:
            os.rename(path, bak)
        except OSError:
            pass
        return dict(defaults)


def save_inception_state(state, state_path=None):
    """Persist Inception state. Keeps only last 10 runs."""
    path = state_path or INCEPTION_STATE_PATH
    state["runs"] = state.get("runs", [])[-10:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _acquire_pid_lock(path):
    """Acquire an exclusive pid lock file, breaking stale locks."""
    lock_path = Path(path)

    if lock_path.exists():
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age < 600:
                try:
                    pid = int(lock_path.read_text().strip())
                    os.kill(pid, 0)
                    return False
                except (ValueError, OSError):
                    pass
        except OSError:
            pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def acquire_inception_lock(lock_path=None):
    """File-based lock for Inception. Returns True if acquired."""
    return _acquire_pid_lock(lock_path or INCEPTION_LOCK_PATH)


def release_inception_lock(lock_path=None):
    """Release the Inception lock file."""
    path = Path(lock_path or INCEPTION_LOCK_PATH)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def acquire_vault_write_lock(lock_path=None, timeout=5.0, poll_interval=0.05):
    """Acquire a short-lived vault write lock, polling until timeout."""
    deadline = time.monotonic() + timeout
    path = lock_path or VAULT_WRITE_LOCK_PATH
    while True:
        if _acquire_pid_lock(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def release_vault_write_lock(lock_path=None):
    """Release the vault write lock file."""
    path = Path(lock_path or VAULT_WRITE_LOCK_PATH)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _tokenize_for_match(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def find_dedup_candidates(vault_path, title, tags, limit=5):
    """Find notes with title/tag overlap likely to cover the same topic."""
    notes_dir = Path(vault_path) / "notes"
    if not notes_dir.exists():
        return []

    title_tokens = _tokenize_for_match(title)
    tag_tokens = {tag.lower() for tag in tags}
    ranked = []

    for note_path in notes_dir.glob("*.md"):
        try:
            text = note_path.read_text()
        except OSError:
            continue

        title_match = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
        note_title = title_match.group(1).strip() if title_match else note_path.stem
        note_tokens = _tokenize_for_match(note_title)
        overlap = len(title_tokens & note_tokens)

        tag_match = re.search(r"^tags:\s*\[([^\]]*)\]", text, re.MULTILINE)
        note_tags = set()
        if tag_match:
            note_tags = {
                token.strip().strip('"').strip("'").lower() for token in tag_match.group(1).split(",") if token.strip()
            }
        overlap += len(tag_tokens & note_tags)

        if overlap > 0:
            ranked.append((overlap, note_path))

    ranked.sort(key=lambda item: (-item[0], item[1].name))
    return [path for _, path in ranked[:limit]]


def write_note(
    vault_path,
    title,
    body,
    note_type,
    tags,
    certainty=None,
    source="session",
    validity_context=None,
    supersedes=None,
    project=None,
    branch=None,
    session_id=None,
):
    """Write an atomic note with frontmatter to notes/ using an atomic rename."""
    notes_dir = Path(vault_path) / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    slug = slugify(title)
    target = notes_dir / f"{slug}.md"
    tmp = notes_dir / f".tmp-{slug}.md"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")

    lines = [
        "---",
        f"title: {title}",
        f"type: {note_type}",
        f"tags: [{', '.join(tags)}]",
        f"source: {source}",
    ]
    if certainty is not None:
        lines.append(f"certainty: {certainty}")
    if validity_context:
        lines.append(f"validity-context: {validity_context}")
    if supersedes:
        lines.append(f'supersedes: "{supersedes}"')
    if project:
        lines.append(f"project: {project}")
    if branch:
        lines.append(f"branch: {branch}")
    lines.append(f"date: {now}")
    if session_id:
        lines.append(f"session_id: {session_id}")
    lines.extend(["---", "", body.strip(), "", "## Related", ""])

    tmp.write_text("\n".join(lines))
    os.replace(tmp, target)
    return target


def update_project_index(vault_path, project_slug, note_name, session_summary):
    """Ensure project index exists and append note/session references."""
    project_dir = Path(vault_path) / "projects"
    project_dir.mkdir(parents=True, exist_ok=True)
    project_file = project_dir / f"{project_slug}.md"

    if project_file.exists():
        content = project_file.read_text()
    else:
        content = "\n".join(
            [
                "---",
                f"title: {project_slug}",
                f"project: {project_slug}",
                "---",
                "",
                "## Notes",
                "",
                "## Sessions",
                "",
            ]
        )

    note_line = f"- [[{note_name}]]"
    if note_line not in content:
        if "## Notes" in content:
            notes_pos = content.index("## Notes") + len("## Notes")
            sessions_pos = content.find("\n## Sessions", notes_pos)
            if sessions_pos == -1:
                content = content.rstrip() + "\n" + note_line + "\n"
            else:
                before = content[:sessions_pos].rstrip()
                after = content[sessions_pos:]
                content = before + "\n" + note_line + "\n\n" + after.lstrip("\n")
        else:
            content = content.rstrip() + "\n\n## Notes\n\n" + note_line + "\n"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session_line = f"- {today} {session_summary}"
    if "## Sessions" in content:
        content = content.rstrip() + "\n" + session_line + "\n"
    else:
        content = content.rstrip() + "\n\n## Sessions\n\n" + session_line + "\n"

    project_file.write_text(content)
