"""Claude Code transcript parser.

Parses the JSONL transcript format produced by Claude Code sessions.
Each line is a JSON object with type "user" or "assistant".
"""

import json
import re

from memento.utils import sanitize_secrets


def parse_transcript(transcript_path):
    """Parse a Claude Code JSONL transcript file.

    Returns:
        Dict with session metadata: cwd, git_branch, exchange_count,
        user_messages, files_edited, files_read, first_prompt, last_outcome.
    """
    user_count = 0
    assistant_count = 0
    files_edited = set()
    files_read = set()
    git_branch = None
    cwd = None
    first_user_prompt = None
    last_assistant_text = None

    with open(transcript_path) as f:
        for line in f:
            entry = json.loads(line)
            msg_type = entry.get("type")
            cwd = cwd or entry.get("cwd")
            git_branch = git_branch or entry.get("gitBranch")

            if msg_type == "user":
                user_count += 1
                msg = entry.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str) and not first_user_prompt:
                    # Strip system tags from prompt text
                    cleaned = re.sub(r"<[^>]+>.*?</[^>]+>", "", content, flags=re.DOTALL).strip()
                    if cleaned:
                        first_user_prompt = sanitize_secrets(cleaned[:200])

            elif msg_type == "assistant":
                assistant_count += 1
                msg = entry.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text = block.get("text", "").strip()
                            if text:
                                last_assistant_text = text
                        elif block.get("type") == "tool_use":
                            name = block.get("name", "")
                            inp = block.get("input", {})
                            if name in ("Edit", "Write"):
                                fp = inp.get("file_path", "")
                                if fp:
                                    files_edited.add(fp)
                            elif name == "Read":
                                fp = inp.get("file_path", "")
                                if fp:
                                    files_read.add(fp)

    # Extract first sentence of last assistant text as session outcome
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
        "git_branch": git_branch,
        "exchange_count": exchange_count,
        "user_messages": user_count,
        "files_edited": sorted(files_edited),
        "files_read": sorted(files_read),
        "first_prompt": first_user_prompt,
        "last_outcome": last_outcome,
    }
