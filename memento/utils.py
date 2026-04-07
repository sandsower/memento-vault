"""Secret sanitization, tag normalization, and hook I/O helpers."""

import json
import re
import sys
from pathlib import Path

from memento.config import get_config

# --- Secret sanitization ---

_SECRET_PATTERNS = [
    # API keys and tokens
    (r"(sk-[a-zA-Z0-9]{20,})", "[REDACTED_API_KEY]"),
    (r"(sk-proj-[a-zA-Z0-9_-]{20,})", "[REDACTED_API_KEY]"),
    (r"(ghp_[a-zA-Z0-9]{36,})", "[REDACTED_GITHUB_TOKEN]"),
    (r"(gho_[a-zA-Z0-9]{36,})", "[REDACTED_GITHUB_TOKEN]"),
    (r"(github_pat_[a-zA-Z0-9_]{20,})", "[REDACTED_GITHUB_TOKEN]"),
    (r"(xoxb-[a-zA-Z0-9\-]+)", "[REDACTED_SLACK_TOKEN]"),
    (r"(xoxp-[a-zA-Z0-9\-]+)", "[REDACTED_SLACK_TOKEN]"),
    (r"(AKIA[0-9A-Z]{16})", "[REDACTED_AWS_KEY]"),
    (r"(eyJ[a-zA-Z0-9_\-]{10,}\.eyJ[a-zA-Z0-9_\-]{10,})", "[REDACTED_JWT]"),
    # Connection strings
    (r'((?:postgres|mysql|mongodb|redis)://[^\s"\'`]+)', "[REDACTED_CONNECTION_STRING]"),
    # Bearer tokens
    (r"(Bearer\s+[a-zA-Z0-9_\-.]{20,})", "Bearer [REDACTED_TOKEN]"),
    # Generic high-entropy secrets (env var assignments)
    (r'(?:_KEY|_SECRET|_TOKEN|_PASSWORD|_PASS)\s*[=:]\s*["\']?([a-zA-Z0-9_\-/.]{20,})["\']?', "[REDACTED_SECRET]"),
]
_COMPILED_SECRET_PATTERNS = [(re.compile(p, re.IGNORECASE), r) for p, r in _SECRET_PATTERNS]


def sanitize_secrets(text):
    """Redact common secret patterns from text.

    Returns the sanitized text. Applied to fleeting notes, project indexes,
    and injected into the agent prompt for atomic note generation.
    """
    if not text:
        return text
    for pattern, replacement in _COMPILED_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# --- Tag normalization ---


def normalize_tags(tags):
    """Normalize a list of tags using the configured alias map.

    Lowercases all tags and replaces aliases with canonical forms.
    Returns deduplicated list preserving order.
    """
    config = get_config()
    aliases = config.get("tag_aliases", {})

    seen = set()
    normalized = []
    for tag in tags:
        tag = tag.lower().strip()
        tag = aliases.get(tag, tag)
        if tag and tag not in seen:
            seen.add(tag)
            normalized.append(tag)
    return normalized


def normalize_note_tags(note_path):
    """Read a note file, normalize its frontmatter tags, rewrite if changed.

    Returns True if the file was modified, False otherwise.
    """
    path = Path(note_path)
    if not path.exists() or not path.suffix == ".md":
        return False

    content = path.read_text()
    if not content.startswith("---"):
        return False

    # Find frontmatter closing fence: must be a standalone "---" line
    lines = content.split("\n")
    end_line = None
    for i, line in enumerate(lines):
        if i == 0:
            continue  # skip opening fence
        if line.strip() == "---":
            end_line = i
            break
    if end_line is None:
        return False
    # Reconstruct byte offset: everything up to and including the closing fence line
    end = sum(len(l) + 1 for l in lines[: end_line + 1])

    frontmatter = content[:end].rstrip("\n")
    body = content[end:]

    # Extract tags line
    tag_match = re.search(r"^(tags:\s*)\[([^\]]*)\]", frontmatter, re.MULTILINE)
    if not tag_match:
        return False

    prefix = tag_match.group(1)
    raw_tags = [t.strip().strip('"').strip("'") for t in tag_match.group(2).split(",")]
    raw_tags = [t for t in raw_tags if t]

    normalized = normalize_tags(raw_tags)

    if raw_tags == normalized:
        return False

    new_tag_line = f"{prefix}[{', '.join(normalized)}]"
    new_frontmatter = frontmatter[: tag_match.start()] + new_tag_line + frontmatter[tag_match.end() :]
    path.write_text(new_frontmatter + body)
    return True


# --- Hook I/O helpers ---


def read_hook_input():
    """Read JSON from stdin (hook event data)."""
    raw = sys.stdin.read()
    return json.loads(raw)
