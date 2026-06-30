#!/usr/bin/env python3
"""
Vault briefing — SessionStart hook.

Thin Claude Code adapter over memento.lifecycle build/worker functions.
"""

import sys
from pathlib import Path

# Allow imports from the repo
_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root))

from memento.lifecycle import build_briefing, run_deferred_briefing_search  # noqa: E402
from memento.store import log_retrieval  # noqa: E402
from memento.utils import read_hook_input  # noqa: E402


def main() -> None:
    if "--deferred" in sys.argv:
        deferred_path = None
        if "--deferred-path" in sys.argv:
            index = sys.argv.index("--deferred-path")
            if index + 1 < len(sys.argv):
                deferred_path = sys.argv[index + 1]
        run_deferred_briefing_search(deferred_path)
        sys.exit(0)

    try:
        hook_input = read_hook_input()
    except Exception as exc:
        log_retrieval("briefing", "hook_input_failed", error=str(exc))
        sys.exit(0)

    result = build_briefing(
        hook_input.get("cwd", ""),
        hook_input.get("session_id", "unknown"),
        host_id="claude",
    )
    if result.should_inject:
        print(result.content)


if __name__ == "__main__":
    main()
