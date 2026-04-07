#!/usr/bin/env python3
"""
Shared utilities for memento-vault hooks.

This module re-exports from the memento package for backwards compatibility.
New code should import from memento.config, memento.search, etc. directly.
"""

import json
import os
import sys
from pathlib import Path

# Add the repo root to sys.path so `import memento` works from hooks/
_repo_root = str(Path(__file__).parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from memento.config import (  # noqa: E402, F401
    DEFAULT_CONFIG,
    RUNTIME_DIR,
    detect_project,
    get_config,
    get_runtime_dir,
    get_vault,
    load_config,
    slugify,
)
from memento.graph import (  # noqa: E402, F401
    _GRAPH_CACHE,
    _deserialize_graph,
    _serialize_graph,
    apply_pagerank_boost,
    build_wikilink_graph,
    compute_pagerank,
    extract_wikilinks,
    load_concept_index,
    load_or_build_graph,
    load_project_maps,
    lookup_concepts,
    lookup_project_notes,
    note_is_superseded,
    ppr_expand,
    read_note_metadata,
)
from memento.search import (  # noqa: E402, F401
    VSEARCH_WARM_PATH,
    _clean_snippet,
    _extract_expansion_terms,
    apply_temporal_decay,
    enhance_results,
    expand_wikilinks,
    filter_by_project,
    has_qmd,
    is_vsearch_warm,
    mark_vsearch_warm,
    multi_hop_search,
    prf_expand_query,
    qmd_get,
    qmd_search,
    qmd_search_with_extras,
    rrf_fuse,
)
from memento.utils import (  # noqa: E402, F401
    _COMPILED_SECRET_PATTERNS,
    _SECRET_PATTERNS,
    normalize_note_tags,
    normalize_tags,
    read_hook_input,
    sanitize_secrets,
)

# --- Retrieval logging ---

RETRIEVAL_LOG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
    "memento-vault",
    "retrieval.jsonl",
)


def _should_log():
    """Check if retrieval logging is enabled (config or env var)."""
    if os.environ.get("MEMENTO_DEBUG"):
        return True
    return get_config().get("retrieval_log", False)


def log_retrieval(hook, action, **kwargs):
    """Append a structured log entry to the retrieval log."""
    if not _should_log():
        return

    import time as _time

    entry = {
        "ts": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "hook": hook,
        "action": action,
    }
    entry.update(kwargs)

    try:
        log_dir = os.path.dirname(RETRIEVAL_LOG_PATH)
        os.makedirs(log_dir, exist_ok=True)
        with open(RETRIEVAL_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


# --- Inception (state management) ---

INCEPTION_STATE_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
    "memento-vault",
    "inception-state.json",
)


def load_inception_state(state_path=None):
    """Load Inception state from disk. Returns defaults if missing/corrupt."""
    path = state_path or INCEPTION_STATE_PATH
    defaults = {
        "last_run_iso": None,
        "last_run_note_count": 0,
        "runs": [],
        "processed_notes": [],
    }
    try:
        with open(path) as f:
            state = json.load(f)
        for k, v in defaults.items():
            state.setdefault(k, v)
        return state
    except FileNotFoundError:
        return dict(defaults)
    except (json.JSONDecodeError, KeyError):
        bak = path + ".bak"
        try:
            os.rename(path, bak)
        except OSError:
            pass
        return dict(defaults)


def save_inception_state(state, state_path=None):
    """Persist Inception state. Keeps only last 10 runs."""
    path = state_path or INCEPTION_STATE_PATH
    state["runs"] = state.get("runs", [])[-10:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


# --- Inception (lock management) ---

INCEPTION_LOCK_PATH = os.path.join(RUNTIME_DIR, "inception.lock")


def acquire_inception_lock(lock_path=None):
    """File-based lock for Inception. Returns True if acquired."""
    import time as _time

    path = Path(lock_path or INCEPTION_LOCK_PATH)

    if path.exists():
        try:
            age = _time.time() - path.stat().st_mtime
            if age < 600:
                try:
                    pid = int(path.read_text().strip())
                    os.kill(pid, 0)
                    return False
                except (ValueError, OSError):
                    pass
        except OSError:
            pass
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def release_inception_lock(lock_path=None):
    """Release the Inception lock file."""
    path = Path(lock_path or INCEPTION_LOCK_PATH)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
