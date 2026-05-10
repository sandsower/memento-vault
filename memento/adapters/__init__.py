"""Transcript parsing adapters for different coding agents.

Each adapter implements parse_transcript(path) -> SessionMeta dict.
The dispatcher detects the agent from the transcript format or env vars
and routes to the appropriate adapter.
"""

import json
import os

from memento.adapters.claude import parse_transcript as _parse_claude

_SNIFF_MAX_LINES = 20


def detect_agent(transcript_path):
    """Detect which agent produced a transcript file.

    Detection order:
    1. MEMENTO_AGENT env var (explicit override)
    2. Sniff the first line of the transcript for format clues

    Returns one of: "claude", "codex", "cursor", "windsurf", "unknown"
    """
    env_agent = os.environ.get("MEMENTO_AGENT", "").lower().strip()
    if env_agent in ("claude", "codex", "cursor", "windsurf"):
        return env_agent

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


def parse_transcript(transcript_path, agent=None):
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
    elif agent in ("codex", "cursor", "windsurf"):
        raise ValueError(
            f"Transcript parsing for {agent!r} is not yet implemented. "
            "Use memento_capture with session_summary instead of transcript_path."
        )
    else:
        raise ValueError(f"Unknown agent: {agent!r}. Set MEMENTO_AGENT env var or use a supported format.")

    meta["agent"] = agent
    return meta
