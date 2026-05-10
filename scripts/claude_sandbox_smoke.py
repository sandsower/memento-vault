#!/usr/bin/env python3
"""Strict smoke test for memento's sandboxed Claude CLI worker.

This intentionally exercises the same shared Claude backend used by detached
SessionEnd structured-note extraction. It is a local Beislið gate, not a
portable CI test: if Claude auth/config is broken on the shipping machine, the
gate should fail before release.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memento.llm import llm_complete


def main() -> int:
    result = llm_complete(
        'Return JSON only, exactly: {"ok": true}',
        {"llm_backend": "claude", "llm_model": None},
        timeout=60,
    )
    if not result.ok:
        print(f"claude sandbox smoke failed: {result.error}", file=sys.stderr)
        return 1

    try:
        payload = json.loads(result.text)
    except json.JSONDecodeError as exc:
        print(f"claude sandbox smoke returned non-JSON: {exc}: {result.text[:200]}", file=sys.stderr)
        return 1

    if payload != {"ok": True}:
        print(f"claude sandbox smoke returned unexpected payload: {payload!r}", file=sys.stderr)
        return 1

    print("claude sandbox smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
