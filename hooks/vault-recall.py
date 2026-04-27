#!/usr/bin/env python3
"""
Vault recall — UserPromptSubmit hook.

Thin Claude Code adapter over memento.lifecycle build/worker functions.
"""

import sys
from pathlib import Path

# Allow imports from the repo
_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root))

from memento.lifecycle import (  # noqa: E402
    build_recall,
    consume_deep_recall,
    consume_deferred_briefing,
    run_deep_recall_worker,
)
from memento.store import log_retrieval  # noqa: E402
from memento.utils import read_hook_input  # noqa: E402


def main() -> None:
    # Handle background worker mode
    if len(sys.argv) >= 4 and sys.argv[1] == "--deep-recall":
        run_deep_recall_worker(sys.argv[2], sys.argv[3])
        return

    deferred_lines = consume_deferred_briefing()
    deep_recall_lines = consume_deep_recall()

    try:
        hook_input = read_hook_input()
    except Exception as exc:
        log_retrieval("recall", "hook_input_failed", error=str(exc))
        hook_input = {}

    result = build_recall(
        hook_input.get("prompt", ""),
        hook_input.get("cwd", ""),
        hook_input.get("session_id", "unknown"),
    )

    output = deferred_lines + deep_recall_lines
    if result.should_inject:
        output.append(result.content)
    if output:
        print("\n".join(output))


if __name__ == "__main__":
    main()
