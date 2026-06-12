"""Transcript parsing adapters for different coding agents.

Each adapter implements parse_transcript(path) -> SessionMeta dict.
The dispatcher detects the agent from the transcript format or env vars
and routes to the appropriate adapter.
"""

import json
import os

from memento.adapters.claude import parse_transcript as _parse_claude
from memento.adapters.opencode import looks_like_opencode_db
from memento.adapters.opencode import parse_transcript as _parse_opencode
from memento.adapters.opencode import render_transcript_text as _render_opencode

_SNIFF_MAX_LINES = 20


def detect_agent(transcript_path):
    """Detect which agent produced a transcript file.

    Detection order:
    1. MEMENTO_AGENT env var (explicit override)
    2. Sniff the file: SQLite header → OpenCode; JSONL first records → Claude

    Returns one of: "claude", "opencode", "codex", "cursor", "windsurf", "unknown"
    """
    env_agent = os.environ.get("MEMENTO_AGENT", "").lower().strip()
    if env_agent in ("claude", "opencode", "codex", "cursor", "windsurf"):
        return env_agent

    # OpenCode stores sessions in SQLite, so check the binary header first;
    # otherwise opening it as text would just raise UnicodeDecodeError below.
    if looks_like_opencode_db(transcript_path):
        return "opencode"

    # Sniff transcript format by scanning early records. Claude Code writes
    # metadata records (file-history-snapshot, attachment, system) ahead of
    # the first user/assistant message, so checking only line 1 is unreliable.
    try:
        with open(transcript_path) as f:
            for _ in range(_SNIFF_MAX_LINES):
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and data.get("type") in ("user", "assistant"):
                    return "claude"

            # Future: add sniffing for codex, cursor, windsurf formats
            # Codex: TBD
            # Cursor: TBD
            # Windsurf: TBD

    except (UnicodeDecodeError, FileNotFoundError):
        pass
    except OSError:
        # Let real file access errors (permissions, disk) propagate rather than
        # hiding them behind a misleading "unknown agent" error.
        raise

    return "unknown"


def render_transcript_text(transcript_path, agent=None, session_id=None):
    """Render a transcript as readable text for LLM prompts.

    Text transcript formats fall back to reading the file. Binary transcript
    stores such as OpenCode SQLite DBs are rendered by their adapter.
    """
    if agent is None:
        agent = detect_agent(transcript_path)

    if agent == "opencode":
        return _render_opencode(transcript_path, session_id=session_id)
    return open(transcript_path).read()


_TRUNCATION_MARKER_RESERVE = 120


def truncate_transcript(text, max_chars):
    """Head+tail truncate transcript text to a character budget.

    Keeps the opening (goal, context) and the tail (outcomes, decisions),
    dropping the middle with an explicit elision marker. Session endings
    carry most of the durable signal, so the tail gets the larger share.
    Returns text unchanged when it fits or when max_chars <= 0 (disabled).
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    budget = max_chars - _TRUNCATION_MARKER_RESERVE
    if budget < 200:
        return text[-max_chars:]

    head_budget = budget // 4
    tail_budget = budget - head_budget
    head = text[:head_budget]
    tail = text[-tail_budget:]

    # Cut on line boundaries so the LLM never sees a spliced half-record.
    cut = head.rfind("\n")
    if cut > 0:
        head = head[: cut + 1]
    cut = tail.find("\n")
    if cut >= 0:
        tail = tail[cut + 1 :]

    elided = len(text) - len(head) - len(tail)
    marker = f"\n[... transcript truncated: {elided:,} characters elided from the middle ...]\n"
    return head + marker + tail


def parse_transcript(transcript_path, agent=None, session_id=None):
    """Parse a transcript file using the appropriate agent adapter.

    Args:
        transcript_path: Path to the transcript file.
        agent: Agent name override. If None, auto-detects from file format.

    Returns:
        Dict with standardized session metadata:
        - cwd: str | None
        - git_branch: str | None
        - exchange_count: int
        - user_messages: int
        - files_edited: list[str]
        - files_read: list[str]
        - first_prompt: str | None
        - last_outcome: str | None
        - agent: str (which agent produced this transcript)

    Raises:
        ValueError: If the agent is unknown and can't be detected.
    """
    if agent is None:
        agent = detect_agent(transcript_path)

    if agent == "claude":
        meta = _parse_claude(transcript_path)
    elif agent == "opencode":
        meta = _parse_opencode(transcript_path, session_id=session_id)
    elif agent in ("codex", "cursor", "windsurf"):
        raise ValueError(
            f"Transcript parsing for {agent!r} is not yet implemented. "
            "Use memento_capture with session_summary instead of transcript_path."
        )
    else:
        raise ValueError(f"Unknown agent: {agent!r}. Set MEMENTO_AGENT env var or use a supported format.")

    meta["agent"] = agent
    return meta
