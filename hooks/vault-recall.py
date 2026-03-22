#!/usr/bin/env python3
"""
Vault recall — UserPromptSubmit hook.
Runs JIT semantic search against the user's prompt and prints
relevant vault notes to stdout so Claude sees them before processing.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

# Allow imports from the same directory
sys.path.insert(0, str(Path(__file__).parent))

from memento_utils import get_config, get_vault, has_qmd, qmd_search_with_extras, read_hook_input

LAST_RECALL_PATH = "/tmp/memento-last-recall.json"


def should_skip(prompt, config):
    """Relevance gate — returns True if we should skip vault injection."""
    prompt = prompt.strip()

    # Too short
    if len(prompt) < 10:
        return True

    # Skill invocation
    if prompt.startswith("/"):
        return True

    # Match skip patterns
    skip_patterns = config.get("recall_skip_patterns", [])
    prompt_lower = prompt.lower().strip()
    for pattern in skip_patterns:
        try:
            if re.match(pattern, prompt_lower, re.IGNORECASE):
                return True
        except re.error:
            continue

    return False


def is_duplicate(top_result_path):
    """Check if the top result is the same as the last injection.
    Returns True if we should skip to avoid repetition.
    """
    try:
        if not os.path.exists(LAST_RECALL_PATH):
            return False

        with open(LAST_RECALL_PATH) as f:
            last = json.load(f)

        # Skip if same top result within the last 3 prompts
        if last.get("top_path") == top_result_path:
            prompts_since = last.get("prompts_since", 0)
            if prompts_since < 3:
                # Update counter
                last["prompts_since"] = prompts_since + 1
                with open(LAST_RECALL_PATH, "w") as f:
                    json.dump(last, f)
                return True

        return False

    except Exception:
        return False


def record_recall(top_result_path):
    """Record this recall for dedup tracking."""
    try:
        with open(LAST_RECALL_PATH, "w") as f:
            json.dump({
                "top_path": top_result_path,
                "prompts_since": 0,
                "timestamp": time.time(),
            }, f)
    except Exception:
        pass


def bump_prompts_since():
    """Increment the prompts_since counter when we skip injection."""
    try:
        if not os.path.exists(LAST_RECALL_PATH):
            return

        with open(LAST_RECALL_PATH) as f:
            last = json.load(f)

        last["prompts_since"] = last.get("prompts_since", 0) + 1
        with open(LAST_RECALL_PATH, "w") as f:
            json.dump(last, f)

    except Exception:
        pass


def format_result(result):
    """Format a QMD result as a compact one-liner."""
    title = result.get("title", "")
    snippet = result.get("snippet", "").strip()

    # Truncate snippet to first sentence or 120 chars
    if snippet:
        dot = snippet.find(".")
        if 0 < dot < 120:
            snippet = snippet[:dot + 1]
        elif len(snippet) > 120:
            snippet = snippet[:120] + "..."

    line = f"  - {title}"
    if snippet:
        line += f": {snippet}"
    return line


def main():
    config = get_config()

    if not config.get("prompt_recall", True):
        sys.exit(0)

    vault = get_vault()
    if not vault.exists() or not (vault / "notes").exists():
        sys.exit(0)

    if not has_qmd():
        sys.exit(0)

    try:
        hook_input = read_hook_input()
    except Exception:
        sys.exit(0)

    prompt = hook_input.get("prompt", "")
    if not prompt:
        sys.exit(0)

    if should_skip(prompt, config):
        bump_prompts_since()
        sys.exit(0)

    # Semantic search against the prompt
    min_score = config.get("recall_min_score", 0.4)
    max_notes = config.get("recall_max_notes", 3)

    results = qmd_search_with_extras(
        prompt,
        limit=max_notes + 2,  # overfetch for dedup
        semantic=True,
        timeout=12,
        min_score=min_score,
    )

    if not results:
        bump_prompts_since()
        sys.exit(0)

    # Dedup check against last recall
    top_path = results[0].get("path", "")
    if is_duplicate(top_path):
        sys.exit(0)

    # Format and print
    lines = ["[vault] Related memories:"]
    for result in results[:max_notes]:
        lines.append(format_result(result))

    print("\n".join(lines))

    # Record for dedup
    record_recall(top_path)


if __name__ == "__main__":
    main()
