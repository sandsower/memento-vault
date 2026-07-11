#!/usr/bin/env python3
"""
Vault tool context — PreToolUse hook.

Thin Claude Code adapter over memento.lifecycle.build_tool_context.
"""

import json
import sys
from pathlib import Path

# Allow imports from the repo
_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root))

from memento.config import get_config  # noqa: E402
from memento.lifecycle import build_tool_context  # noqa: E402
from memento.store import log_retrieval  # noqa: E402
from memento.trust import frame_untrusted_context  # noqa: E402
from memento.utils import read_hook_input  # noqa: E402


def output_context(context_text: str) -> None:
    """Print the PreToolUse JSON response with additionalContext."""
    max_content_chars = get_config().get("automatic_context_max_chars", 4000)
    framed = frame_untrusted_context(
        context_text,
        surface="tool-context",
        max_content_chars=max_content_chars,
    )
    if not framed:
        return
    response = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": framed,
        }
    }
    print(json.dumps(response))


def main() -> None:
    try:
        hook_input = read_hook_input()
    except Exception as exc:
        log_retrieval("tool-context", "hook_input_failed", error=str(exc))
        sys.exit(0)

    # The transcript file is stable across session resumes while session_id
    # is not; use its stem as the injection-cap lineage when available.
    transcript_path = hook_input.get("transcript_path") or ""
    lineage_id = Path(transcript_path).stem if transcript_path else None
    result = build_tool_context(
        hook_input.get("tool_name", ""),
        hook_input.get("tool_input", {}).get("file_path", ""),
        hook_input.get("cwd", ""),
        hook_input.get("session_id", "unknown"),
        lineage_id=lineage_id,
        host_id="claude",
    )
    if result.should_inject:
        output_context(result.content)


if __name__ == "__main__":
    main()
