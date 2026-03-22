#!/usr/bin/env python3
"""
Shared utilities for memento-vault hooks.
Config loading, project detection, QMD queries.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


# --- Configuration ---

DEFAULT_CONFIG = {
    "vault_path": str(Path.home() / "memento"),
    "exchange_threshold": 15,
    "file_count_threshold": 3,
    "notable_patterns": ["plan", "design", "MEMORY.md", "CLAUDE.md", "SKILL.md"],
    "qmd_collection": "memento",
    "extra_qmd_collections": [],
    "project_rules": [],
    "auto_commit": True,
    "agent_model": "sonnet",
    "agent_delay_seconds": 90,
    # Retrieval hooks
    "session_briefing": True,
    "briefing_max_notes": 5,
    "briefing_min_score": 0.55,
    "prompt_recall": True,
    "recall_min_score": 0.6,
    "recall_max_notes": 3,
    "recall_skip_patterns": [
        r"^(yes|no|ok|sure|thanks|y|n|yep|nope|looks good|lgtm|ship it|continue)$",
        r"^git\s",
        r"^run\s",
    ],
    # Tool context hook (PreToolUse)
    "tool_context": True,
    "tool_context_min_score": 0.75,
    "tool_context_max_notes": 2,
    "tool_context_max_injections": 5,
    "tool_context_cooldown": 3,
}

_CONFIG = None


def load_config():
    """Load config from memento.yml, falling back to defaults."""
    config = dict(DEFAULT_CONFIG)

    candidates = [
        Path.home() / ".config" / "memento-vault" / "memento.yml",
        Path.home() / ".memento-vault.yml",
    ]

    vault_path = Path(config["vault_path"])
    if vault_path.exists():
        candidates.insert(0, vault_path / "memento.yml")

    for path in candidates:
        if path.exists():
            try:
                try:
                    import yaml
                    with open(path) as f:
                        user_config = yaml.safe_load(f) or {}
                except ImportError:
                    user_config = _parse_simple_yaml(path)

                config.update({k: v for k, v in user_config.items() if v is not None})
            except Exception:
                pass
            break

    config["vault_path"] = str(Path(config["vault_path"]).expanduser())

    # Handle floats that simple YAML parser returns as strings
    for key in ("briefing_min_score", "recall_min_score"):
        if isinstance(config.get(key), str):
            try:
                config[key] = float(config[key])
            except (ValueError, TypeError):
                pass

    return config


def _parse_simple_yaml(path):
    """Minimal YAML parser for simple key: value configs. No nested structures."""
    result = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                if value.lower() in ("true", "yes"):
                    value = True
                elif value.lower() in ("false", "no"):
                    value = False
                elif value.isdigit():
                    value = int(value)
                elif value.startswith("[") and value.endswith("]"):
                    value = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",")]
                elif (value.startswith('"') and value.endswith('"')) or \
                     (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                result[key] = value
    return result


def get_config():
    """Get cached config."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def get_vault():
    """Get vault path."""
    return Path(get_config()["vault_path"])


# --- Project detection ---


def slugify(text):
    """Simple slug from text."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text)
    return text[:80]


def detect_project(cwd, git_branch):
    """Derive a project slug and optional ticket from cwd and branch.
    Returns (project_slug, ticket_or_none).
    """
    if not cwd:
        return "unknown", None

    config = get_config()
    rules = config.get("project_rules", [])

    for rule in rules:
        if isinstance(rule, dict) and rule.get("path_contains") and rule["path_contains"] in cwd:
            ticket = None
            if git_branch and rule.get("ticket_pattern"):
                match = re.search(rule["ticket_pattern"], git_branch, re.IGNORECASE)
                if match:
                    ticket = match.group(1).upper() if match.lastindex else match.group(0).upper()
            return rule.get("slug", slugify(Path(cwd).name)), ticket

    ticket = None
    if git_branch:
        match = re.search(r"([a-z]+-\d+)", git_branch, re.IGNORECASE)
        if match:
            ticket = match.group(1).upper()

    return slugify(Path(cwd).name) or "misc", ticket


# --- QMD wrapper ---


def has_qmd():
    """Check if QMD is installed."""
    return bool(shutil.which("qmd"))


def _clean_snippet(raw):
    """Clean QMD snippet: strip chunk markers, frontmatter, and collapse whitespace."""
    if not raw:
        return ""
    # Remove QMD chunk position markers like "@@ -3,4 @@ (2 before, 12 after)"
    text = re.sub(r"@@ [^@]+ @@\s*\([^)]*\)\s*", "", raw)
    # Remove YAML frontmatter lines (key: value at start)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        # Skip frontmatter-like lines and empty/separator lines
        if stripped == "---" or (": " in stripped and not stripped.startswith("-")):
            continue
        if stripped:
            lines.append(stripped)
    text = " ".join(lines)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200]


def qmd_search(query, collection=None, limit=5, semantic=False, timeout=10, min_score=0.0):
    """Run a QMD search via CLI.

    Args:
        query: Search query string
        collection: QMD collection name (default: from config)
        limit: Max results
        semantic: If True, use vsearch (vector); otherwise search (BM25)
        timeout: Subprocess timeout in seconds
        min_score: Minimum relevance score (0.0-1.0)

    Returns:
        List of dicts with keys: path, title, score, snippet
        Empty list if QMD unavailable or query fails.
    """
    if not query or not query.strip():
        return []

    config = get_config()
    collection = collection or config["qmd_collection"]

    if not has_qmd():
        return []

    cmd_name = "vsearch" if semantic else "search"
    cmd = ["qmd", cmd_name, query, "-c", collection, "-n", str(limit), "--json"]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            return []

        # QMD prints diagnostic lines before JSON — find the JSON start
        stdout = result.stdout
        json_start = stdout.find("[")
        if json_start == -1:
            json_start = stdout.find("{")
        if json_start == -1:
            return []
        data = json.loads(stdout[json_start:])
        results = []

        # QMD JSON output is a list of result objects
        items = data if isinstance(data, list) else data.get("results", [])
        for item in items:
            score = item.get("score", 0.0)
            if score < min_score:
                continue
            # Derive a usable title: prefer file basename over QMD's chunk title
            raw_path = item.get("file", item.get("path", ""))
            # Strip qmd:// URI prefix if present
            if "://" in raw_path:
                raw_path = raw_path.split("://", 1)[1]
                # Remove collection prefix (e.g., "memento/notes/foo.md" -> "notes/foo.md")
                parts = raw_path.split("/", 1)
                if len(parts) > 1:
                    raw_path = parts[1]
            file_title = Path(raw_path).stem
            qmd_title = item.get("title", "")
            if qmd_title and qmd_title not in ("Related", "Notes", "Sessions", ""):
                title = qmd_title
            else:
                title = file_title

            results.append({
                "path": raw_path,
                "title": title,
                "score": score,
                "snippet": _clean_snippet(item.get("snippet", item.get("content", ""))),
            })

        return results[:limit]

    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        return []


def qmd_search_with_extras(query, limit=5, semantic=False, timeout=5, min_score=0.0):
    """Search primary collection + any extra_qmd_collections.

    Returns combined results sorted by score descending.
    """
    config = get_config()
    results = qmd_search(
        query, collection=config["qmd_collection"],
        limit=limit, semantic=semantic, timeout=timeout, min_score=min_score,
    )

    for extra in config.get("extra_qmd_collections", []):
        extra_results = qmd_search(
            query, collection=extra,
            limit=max(3, limit // 2), semantic=semantic, timeout=timeout, min_score=min_score,
        )
        results.extend(extra_results)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


# --- Hook I/O helpers ---


def read_hook_input():
    """Read JSON from stdin (hook event data)."""
    raw = sys.stdin.read()
    return json.loads(raw)
