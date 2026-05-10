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
    except OSError as exc:
        if not getattr(log_retrieval, "_warned", False):
            import sys as _sys

            print(f"[memento] warning: cannot write retrieval log: {exc}", file=_sys.stderr)
            log_retrieval._warned = True


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


def _safe_yaml_scalar(value):
    """Sanitize a value for safe YAML frontmatter interpolation.

    Strips newlines, carriage returns, and leading YAML syntax chars
    to prevent frontmatter injection via multi-line or structured values.
    """
    if value is None:
        return ""
    s = str(value)
    # Collapse to single line
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    # Strip leading YAML block indicators
    s = s.lstrip("-|>")
    return s.strip()


def _tokenize_for_match(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _find_heading_match(content, heading):
    """Return a match for an exact level-2 heading line."""
    return re.search(rf"^{re.escape(heading)}[ \t]*$", content, re.MULTILINE)


def _has_heading(content, heading):
    """Return True when ``content`` contains ``heading`` as its own heading line."""
    return _find_heading_match(content, heading) is not None


def _append_under_heading(content, heading, line):
    """Insert ``line`` at the end of the ``heading`` section's body.

    The section ends at the next level-2 heading or end-of-file. Trailing
    blank lines inside the section are collapsed before the line is added.
    """
    line = line.rstrip("\n")
    heading_match = _find_heading_match(content, heading)
    if heading_match is None:
        return content.rstrip() + "\n\n" + heading + "\n\n" + line + "\n"

    body_start = heading_match.end()
    next_heading_match = re.search(r"^## [^\n]*$", content[body_start:], re.MULTILINE)
    end = len(content) if next_heading_match is None else body_start + next_heading_match.start()

    section = content[body_start:end].rstrip()
    separator = "\n\n" if not section.strip() else "\n"
    new_section = section + separator + line + "\n"
    return content[:body_start] + new_section + content[end:]


def append_project_session_line(content, line):
    """Append an auto-captured session line to the preferred project section.

    Hubs that have opted into ``## Activity log`` keep auto-captures there.
    Older hubs continue to receive entries under ``## Sessions``.
    """
    if _has_heading(content, "## Activity log"):
        return _append_under_heading(content, "## Activity log", line)
    return _append_under_heading(content, "## Sessions", line)


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
    if target.exists():
        # Avoid overwriting — append a numeric suffix
        for i in range(2, 100):
            candidate = notes_dir / f"{slug}-{i}.md"
            if not candidate.exists():
                target = candidate
                break
    tmp = notes_dir / f".tmp-{slug}.md"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")

    # Sanitize all scalar fields to prevent frontmatter injection
    safe_title = _safe_yaml_scalar(title)
    safe_type = _safe_yaml_scalar(note_type)
    safe_source = _safe_yaml_scalar(source)
    safe_tags = [_safe_yaml_scalar(t) for t in tags]

    lines = [
        "---",
        f"title: {safe_title}",
        f"type: {safe_type}",
        f"tags: [{', '.join(safe_tags)}]",
        f"source: {safe_source}",
    ]
    if certainty is not None:
        lines.append(f"certainty: {int(certainty)}")
    if validity_context:
        lines.append(f"validity-context: {_safe_yaml_scalar(validity_context)}")
    if supersedes:
        lines.append(f'supersedes: "{_safe_yaml_scalar(supersedes)}"')
    if project:
        lines.append(f"project: {_safe_yaml_scalar(project)}")
    if branch:
        lines.append(f"branch: {_safe_yaml_scalar(branch)}")
    lines.append(f"date: {now}")
    if session_id:
        lines.append(f"session_id: {_safe_yaml_scalar(session_id)}")
    lines.extend(["---", "", body.strip(), "", "## Related", ""])

    tmp.write_text("\n".join(lines))
    os.replace(tmp, target)

    # Index in embedded search backend if active
    try:
        from memento.search_backend import get_backend
        from memento.embedded_search import EmbeddedSearchBackend

        backend = get_backend()
        if isinstance(backend, EmbeddedSearchBackend):
            rel_path = str(target.relative_to(Path(vault_path)))
            backend.index_note(rel_path)
    except Exception:
        pass  # Indexing failure must not block note storage

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
        if _has_heading(content, "## Notes"):
            content = _append_under_heading(content, "## Notes", note_line)
        else:
            content = content.rstrip() + "\n\n## Notes\n\n" + note_line + "\n"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session_line = f"- {today} {session_summary}"
    if session_line not in content:
        content = append_project_session_line(content, session_line)

    project_file.write_text(content)
