#!/usr/bin/env python3
"""
Vault tool context — PreToolUse hook.
Injects relevant vault notes when Claude reads files in known areas.
Uses directory-level BM25 search with aggressive caching.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

# Allow imports from the same directory
sys.path.insert(0, str(Path(__file__).parent))

from memento_utils import get_config, get_vault, has_qmd, qmd_search_with_extras, enhance_results, read_hook_input

CACHE_PATH = "/tmp/memento-tool-context-cache.json"
RECALL_STATE_PATH = "/tmp/memento-last-recall.json"

# Paths that never have vault knowledge
SKIP_PREFIXES = (
    "/usr/", "/etc/", "/proc/", "/sys/", "/dev/",
    "/tmp/", "/var/", "/snap/",
)

# Directory segments that indicate non-project code
SKIP_SEGMENTS = {
    "node_modules", ".git", "dist", "build", ".next",
    "__pycache__", ".cache", "vendor", ".terraform",
    "target", ".venv", "venv", ".tox", ".mypy_cache",
    ".pytest_cache", "coverage", ".nyc_output",
}

# File extensions that are config/assets, not code with domain knowledge
SKIP_EXTENSIONS = {
    ".json", ".lock", ".yaml", ".yml", ".toml",
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot",
    ".map", ".min.js", ".min.css",
    ".sum", ".mod",
    ".csv", ".xml", ".sql",
    ".env", ".pem", ".key", ".crt",
}

# Specific filenames that match too broadly
SKIP_FILENAMES = {
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "tsconfig.json", "tsconfig.base.json",
    "go.mod", "go.sum", "Cargo.lock", "Cargo.toml",
    ".gitignore", ".prettierrc", ".eslintrc", ".eslintrc.js",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "jest.config.js", "jest.config.ts", "vitest.config.ts",
    ".env", ".env.local", ".env.example",
    "README.md", "CHANGELOG.md", "LICENSE",
}

# Path segments too generic for BM25 queries
STOP_SEGMENTS = {
    "src", "lib", "app", "apps", "cmd", "pkg", "internal",
    "components", "utils", "hooks", "helpers", "services",
    "test", "tests", "__tests__", "spec", "specs",
    "pages", "views", "controllers", "models", "resolvers",
    "middleware", "handlers", "routes", "api",
    "common", "shared", "core", "config", "types",
    "frontend", "backend", "server", "client",
}


def should_skip(file_path, config):
    """Fast exit checks — returns True if this file read should be ignored."""
    # System paths
    for prefix in SKIP_PREFIXES:
        if file_path.startswith(prefix):
            return True

    # Vault files
    vault = get_vault()
    try:
        if os.path.realpath(file_path).startswith(str(vault)):
            return True
    except (OSError, ValueError):
        pass

    # Segment check (any path component is a skip segment)
    parts = Path(file_path).parts
    for part in parts:
        if part in SKIP_SEGMENTS:
            return True

    # Extension check
    ext = Path(file_path).suffix.lower()
    if ext in SKIP_EXTENSIONS:
        return True

    # Filename check
    name = Path(file_path).name
    if name in SKIP_FILENAMES:
        return True

    return False


def extract_keywords(file_path):
    """Extract searchable keywords from a file path for BM25 query."""
    path = file_path

    # Strip home prefix
    home = str(Path.home())
    if path.startswith(home):
        path = path[len(home):]

    parts = Path(path).parts

    words = []
    for part in parts:
        # Skip dot-prefixed dirs and stop segments
        if part.startswith(".") or part in STOP_SEGMENTS:
            continue
        # Strip .git suffix from repo dirs like care.git
        if part.endswith(".git"):
            part = part[:-4]
        # Strip file extension
        if "." in part and part != part.split(".")[0]:
            part = Path(part).stem
        # Split on separators
        tokens = re.split(r"[-_./]", part)
        for token in tokens:
            # Split camelCase
            camel_split = re.sub(r"([a-z])([A-Z])", r"\1 \2", token).split()
            for word in camel_split:
                w = word.lower().strip()
                if len(w) > 1:
                    words.append(w)

    # Deduplicate preserving order
    seen = set()
    unique = []
    for w in words:
        if w not in seen:
            seen.add(w)
            unique.append(w)

    return " ".join(unique)


def load_cache():
    """Load the directory cache from disk."""
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH) as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {"dirs": {}, "last_qmd_call": 0, "injections": {}}


def save_cache(cache):
    """Write the directory cache to disk."""
    try:
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass


def get_recall_paths():
    """Read paths recently injected by vault-recall.py for dedup."""
    try:
        if os.path.exists(RECALL_STATE_PATH):
            with open(RECALL_STATE_PATH) as f:
                data = json.load(f)
            top = data.get("top_path", "")
            return {top} if top else set()
    except (json.JSONDecodeError, OSError):
        pass
    return set()


def session_injection_count(cache, session_id):
    """Count how many notes have been injected this session."""
    return cache.get("injections", {}).get(session_id, {}).get("count", 0)


def session_injected_paths(cache, session_id):
    """Get set of note paths already injected this session."""
    return set(cache.get("injections", {}).get(session_id, {}).get("paths", []))


def record_injection(cache, session_id, note_paths):
    """Record that notes were injected for dedup."""
    if "injections" not in cache:
        cache["injections"] = {}
    if session_id not in cache["injections"]:
        cache["injections"][session_id] = {"count": 0, "paths": []}

    entry = cache["injections"][session_id]
    entry["count"] += len(note_paths)
    entry["paths"].extend(note_paths)


def format_result(result):
    """Format a QMD result as a compact one-liner."""
    title = result.get("title", "")
    snippet = result.get("snippet", "").strip()

    if snippet:
        dot = snippet.find(".")
        if 0 < dot < 100:
            snippet = snippet[: dot + 1]
        elif len(snippet) > 100:
            snippet = snippet[:100] + "..."

    line = f"  - {title}"
    if snippet:
        line += f": {snippet}"
    return line


def output_context(context_text):
    """Print the PreToolUse JSON response with additionalContext."""
    response = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(response))


def main():
    config = get_config()

    if not config.get("tool_context", True):
        sys.exit(0)

    if not has_qmd():
        sys.exit(0)

    try:
        hook_input = read_hook_input()
    except Exception:
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "")
    if tool_name != "Read":
        sys.exit(0)

    file_path = hook_input.get("tool_input", {}).get("file_path", "")
    cwd = hook_input.get("cwd", "")
    if not file_path:
        sys.exit(0)

    # Normalize
    try:
        file_path = os.path.realpath(os.path.expanduser(file_path))
    except (OSError, ValueError):
        sys.exit(0)

    if should_skip(file_path, config):
        sys.exit(0)

    session_id = hook_input.get("session_id", "unknown")

    # Load cache
    cache = load_cache()

    # Check session injection cap
    max_injections = config.get("tool_context_max_injections", 5)
    if session_injection_count(cache, session_id) >= max_injections:
        sys.exit(0)

    # Directory key for caching
    dir_key = str(Path(file_path).parent)

    # Check directory cache
    if dir_key in cache.get("dirs", {}):
        cached = cache["dirs"][dir_key]
        results = cached.get("results", [])
        if not results:
            sys.exit(0)  # Negative cache — we looked and found nothing
    else:
        # Check cooldown
        cooldown = config.get("tool_context_cooldown", 3)
        last_call = cache.get("last_qmd_call", 0)
        if time.time() - last_call < cooldown:
            sys.exit(0)

        # Extract keywords and search
        query = extract_keywords(file_path)
        if not query or len(query.split()) < 2:
            # Too few keywords — would match too broadly
            cache.setdefault("dirs", {})[dir_key] = {"results": []}
            save_cache(cache)
            sys.exit(0)

        min_score = config.get("tool_context_min_score", 0.75)
        max_notes = config.get("tool_context_max_notes", 2)

        results = qmd_search_with_extras(
            query,
            limit=max_notes + 5,  # Overfetch for dedup + enhancement filtering
            semantic=False,  # BM25 for speed
            timeout=2,
            min_score=min_score,
        )

        results = enhance_results(results, config, cwd=cwd)

        # Cache results (even if empty)
        cache["last_qmd_call"] = time.time()
        cache.setdefault("dirs", {})[dir_key] = {"results": results}
        save_cache(cache)

        if not results:
            sys.exit(0)

    # Dedup against recall hook
    recall_paths = get_recall_paths()
    already_injected = session_injected_paths(cache, session_id)
    exclude = recall_paths | already_injected

    filtered = [r for r in results if r.get("path", "") not in exclude]
    if not filtered:
        sys.exit(0)

    # Format and output
    max_notes = config.get("tool_context_max_notes", 2)
    lines = ["[connected-to-vault]"]
    injected_paths = []
    for result in filtered[:max_notes]:
        lines.append(format_result(result))
        injected_paths.append(result.get("path", ""))

    output_context("\n".join(lines))

    # Record injection
    record_injection(cache, session_id, injected_paths)
    save_cache(cache)


if __name__ == "__main__":
    main()
