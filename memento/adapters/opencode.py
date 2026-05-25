"""OpenCode session parser.

Parses an OpenCode SQLite session store. OpenCode keeps sessions in a Drizzle
database with three relevant tables: ``session`` (one row per session),
``message`` (one row per turn, role in JSON ``data``), and ``part`` (content
blocks within a message: text, tool calls, reasoning, lifecycle markers).

Selects which session to parse via the ``MEMENTO_OPENCODE_SESSION_ID``
environment variable, then an explicit ``session_id`` argument; falls back to
the most recently created session.
"""

import json
import os
import re
import sqlite3
from pathlib import Path

from memento.utils import sanitize_secrets

# OpenCode native file tools (lowercase, per upstream tool names).
_EDIT_TOOLS = {"edit", "write", "patch", "apply_patch"}
_READ_TOOLS = {"read"}
_APPLY_PATCH_PATH_RE = re.compile(r"^\*\*\* (Add File|Delete File|Update File|Move to):\s*(.+?)\s*$")
_PROMPT_WRAPPER_RE = re.compile(
    r"^\s*<(?P<tag>(?:prompt|assistant|user|system(?:-[\w-]+)?))\b[^>]*>(?P<body>.*)</(?P=tag)>\s*$",
    re.DOTALL | re.IGNORECASE,
)


def _connect_readonly(db_path):
    """Open the OpenCode SQLite DB read-only so a running OpenCode is unaffected."""
    uri = f"{Path(db_path).expanduser().resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_session_id(conn, override=None):
    """Return the OpenCode session id to parse.

    If ``override`` is given, validate it exists and return it; otherwise return
    the most recently created session. Raises ``ValueError`` when the override
    is unknown or the database has no sessions.
    """
    cur = conn.cursor()
    if override:
        row = cur.execute("SELECT id FROM session WHERE id = ?", (override,)).fetchone()
        if row is None:
            raise ValueError(f"OpenCode session not found: {override!r}")
        return row["id"]
    row = cur.execute("SELECT id FROM session ORDER BY time_created DESC LIMIT 1").fetchone()
    if row is None:
        raise ValueError("OpenCode database contains no sessions")
    return row["id"]


def _first_text_part(parts):
    """Return the first non-empty text part body, or ``None`` if none exist."""
    for part in parts:
        if part.get("type") == "text":
            text = part.get("text", "").strip()
            if text:
                return text
    return None


def _apply_patch_paths(patch_text):
    """Extract edited paths from OpenCode ``apply_patch`` input text."""
    paths = set()
    last_update_path = None
    for line in patch_text.splitlines():
        match = _APPLY_PATCH_PATH_RE.match(line.strip())
        if not match:
            continue
        marker, path = match.groups()
        if marker == "Move to":
            if last_update_path:
                paths.discard(last_update_path)
            paths.add(path)
            last_update_path = None
        else:
            paths.add(path)
            last_update_path = path if marker == "Update File" else None
    return paths


def _clean_prompt_text(text):
    """Strip whole-prompt wrappers without dropping embedded user markup."""
    cleaned = text.strip()
    wrapper = _PROMPT_WRAPPER_RE.match(cleaned)
    if wrapper:
        cleaned = wrapper.group("body").strip()
    return cleaned.strip('"').strip("'")


def parse_transcript(transcript_path, session_id=None):
    """Parse an OpenCode session into the standard session metadata dict.

    Args:
        transcript_path: Path to ``opencode.db`` (typically under
            ``$XDG_DATA_HOME/opencode/``).
        session_id: Specific OpenCode session id (``ses_...``). Overridden by
            ``MEMENTO_OPENCODE_SESSION_ID`` when set; otherwise falls back to
            the most recently created session.

    Returns:
        Dict with session metadata: cwd, git_branch, exchange_count,
        user_messages, files_edited, files_read, first_prompt, last_outcome.
    """
    env_session_id = os.environ.get("MEMENTO_OPENCODE_SESSION_ID")
    if env_session_id:
        session_id = env_session_id

    conn = _connect_readonly(transcript_path)
    try:
        cur = conn.cursor()
        session_id = _resolve_session_id(conn, session_id)

        session = cur.execute("SELECT directory FROM session WHERE id = ?", (session_id,)).fetchone()
        cwd = session["directory"] if session else None

        user_count = 0
        assistant_count = 0
        files_edited = set()
        files_read = set()
        first_user_prompt = None
        last_assistant_text = None

        message_rows = cur.execute(
            "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        ).fetchall()

        for message in message_rows:
            data = json.loads(message["data"])
            role = data.get("role")

            part_rows = cur.execute(
                "SELECT data FROM part WHERE message_id = ? ORDER BY time_created, id",
                (message["id"],),
            ).fetchall()
            parts = [json.loads(p["data"]) for p in part_rows]

            if role == "user":
                user_count += 1
                if first_user_prompt is None:
                    text = _first_text_part(parts)
                    if text:
                        # OpenCode may wrap prompts in surrounding quotes or include
                        # system wrapper tags; preserve ordinary user markup/code.
                        cleaned = _clean_prompt_text(text)
                        if cleaned:
                            first_user_prompt = sanitize_secrets(cleaned[:200])

            elif role == "assistant":
                assistant_count += 1
                for part in parts:
                    ptype = part.get("type")
                    if ptype == "text":
                        text = part.get("text", "").strip()
                        if text:
                            last_assistant_text = text
                    elif ptype == "tool":
                        tool = (part.get("tool") or "").lower()
                        inp = (part.get("state") or {}).get("input") or {}
                        fp = inp.get("file_path") or inp.get("filePath") or inp.get("path")
                        if tool == "apply_patch":
                            files_edited.update(_apply_patch_paths(inp.get("patchText") or inp.get("patch_text") or ""))
                        elif fp and tool in _EDIT_TOOLS:
                            files_edited.add(fp)
                        elif fp and tool in _READ_TOOLS:
                            files_read.add(fp)

        last_outcome = None
        if last_assistant_text:
            last_assistant_text = sanitize_secrets(last_assistant_text)
            dot = last_assistant_text.find(".")
            if 0 < dot < 150:
                last_outcome = last_assistant_text[: dot + 1]
            else:
                last_outcome = last_assistant_text[:100]
                if len(last_assistant_text) > 100:
                    last_outcome += "..."

        exchange_count = min(user_count, assistant_count)
        return {
            "cwd": cwd,
            "git_branch": None,  # OpenCode does not record git branch per-session.
            "exchange_count": exchange_count,
            "user_messages": user_count,
            "files_edited": sorted(files_edited),
            "files_read": sorted(files_read),
            "first_prompt": first_user_prompt,
            "last_outcome": last_outcome,
        }
    finally:
        conn.close()


def looks_like_opencode_db(path):
    """Return True if ``path`` is an SQLite file with OpenCode's session schema.

    Used by :func:`memento.adapters.detect_agent` to sniff SQLite stores without
    importing this module unless needed.
    """
    try:
        with open(path, "rb") as f:
            header = f.read(16)
    except OSError:
        return False
    if not header.startswith(b"SQLite format 3\x00"):
        return False
    try:
        conn = _connect_readonly(path)
    except sqlite3.DatabaseError:
        return False
    try:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('session', 'message', 'part')"
        ).fetchall()
        return len(rows) == 3
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()
