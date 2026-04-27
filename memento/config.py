"""Configuration loading, project detection, vault identity, and runtime paths."""

import json
import os
import re
import tempfile
import uuid
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
    "recall_high_confidence": 0.55,
    "recall_diagnostics": False,
    "recall_diagnostics_include_candidates": False,
    "recall_diagnostics_max_candidates": 10,
    "recall_skip_patterns": [
        r"^(yes|no|ok|sure|thanks|y|n|yep|nope|looks good|lgtm|ship it|continue)$",
        r"^git\s",
        r"^run\s",
    ],
    # PRF (Pseudo-Relevance Feedback) query expansion
    "prf_enabled": True,
    "prf_max_terms": 5,
    "prf_top_docs": 3,
    # Retrieval enhancements
    "temporal_decay": True,
    "temporal_decay_half_life": 90,
    "temporal_decay_certainty_floor": 4,
    "wikilink_expansion": True,
    "wikilink_max_hops": 1,
    "wikilink_score_factor": 0.5,
    "wikilink_max_expanded": 3,
    # Tool context hook (PreToolUse)
    "tool_context": True,
    "tool_context_min_score": 0.65,
    "tool_context_max_notes": 2,
    "tool_context_max_injections": 5,
    "tool_context_cooldown": 1,
    # Inception (background consolidation)
    "inception_enabled": False,
    "inception_backend": "codex",
    "inception_threshold": 5,
    "inception_min_cluster_size": 3,
    "inception_max_clusters": 10,
    "inception_cluster_threshold": 0.7,
    "inception_exclude_tags": [],
    "inception_dry_run": False,
    "inception_pre_reason": True,
    "inception_parallel": 4,
    # Personalized PageRank expansion
    "ppr_enabled": True,
    "ppr_max_expanded": 5,
    "ppr_alpha": 0.85,
    "ppr_min_score": 0.01,
    # PageRank graph boost
    "pagerank_alpha": 0.85,
    "pagerank_boost_weight": 0.3,
    # Project retrieval maps
    "project_maps_enabled": True,
    # Concept index (Tenet)
    "concept_index_enabled": True,
    "concept_index_score": 0.5,
    # RRF (Reciprocal Rank Fusion) hybrid search
    "rrf_enabled": True,
    "rrf_k": 60,
    # Cross-encoder reranking (Tier 2)
    "reranker_enabled": True,
    "reranker_top_k": 10,
    "reranker_model": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "reranker_min_score": 0.01,
    # Multi-hop retrieval (experimental)
    "multi_hop_enabled": False,
    "multi_hop_max": 2,
    # Deep recall — background codex analysis (experimental)
    "deep_recall_enabled": False,
    "deep_recall_backend": "codex",
    # Tag normalization
    "tag_aliases": {
        "k8s": "kubernetes",
        "js": "javascript",
        "ts": "typescript",
        "py": "python",
        "rb": "ruby",
        "db": "database",
        "postgres": "postgresql",
        "mongo": "mongodb",
        "ci": "ci-cd",
        "gh": "github",
        "gha": "github-actions",
        "fe": "frontend",
        "be": "backend",
        "deps": "dependencies",
    },
    # Search backend
    "search_backend": "auto",  # auto | qmd | embedded | grep
    "search_db_path": ".search/search.db",  # relative to vault_path
    # Embedding (for embedded search backend)
    "embedding_provider": "local",  # local | voyage | openai | google
    "embedding_model": "nomic-embed-text-v1.5",
    "embedding_dimensions": 512,
    "embedding_api_key": None,
    "embedding_api_base": None,
    # LLM backend (agent-agnostic)
    "llm_backend": "claude",
    "llm_model": None,  # None = use agent_model for backwards compat
    "llm_api_key": None,
    "llm_api_base": None,
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
            except Exception as exc:
                import sys as _sys

                print(f"[memento] warning: failed to parse config {path}: {exc}", file=_sys.stderr)
            break

    # Environment overrides
    env_vault = os.environ.get("MEMENTO_VAULT_PATH")
    if env_vault:
        config["vault_path"] = env_vault
    env_backend = os.environ.get("MEMENTO_SEARCH_BACKEND")
    if env_backend:
        config["search_backend"] = env_backend
    config["vault_path"] = str(Path(config["vault_path"]).expanduser())

    # Handle floats that simple YAML parser returns as strings
    for key in ("briefing_min_score", "recall_min_score", "inception_cluster_threshold"):
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
                elif (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                result[key] = value
    return result


def get_config():
    """Get cached config."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def reset_config():
    """Reset cached config. Useful for testing."""
    global _CONFIG
    _CONFIG = None


def get_vault():
    """Get vault path."""
    return Path(get_config()["vault_path"])


# --- Vault identity ---

_VAULT_IDENTITY_FILENAME = "vault-identity.json"


def _vault_identity_path():
    """Path to the vault identity file — stored inside the vault itself.

    This ensures the identity is bound to the vault data, not the host config.
    Two vaults on the same machine get different IDs, and moving a vault to
    a new host preserves its identity.
    """
    vault = Path(get_config()["vault_path"])
    return vault / _VAULT_IDENTITY_FILENAME


def _legacy_vault_identity_path():
    """Old location for vault identity (pre-migration)."""
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "memento-vault"
    return config_dir / _VAULT_IDENTITY_FILENAME


def get_vault_id() -> str | None:
    """Get the unique vault ID, creating one on first call.

    The vault ID is a stable UUID that uniquely identifies this vault instance.
    It's generated once and persisted inside the vault directory. Used for
    cross-vault interoperability and note provenance tracking.

    Migrates from the old global config location on first access.
    """
    path = _vault_identity_path()

    # Try current location (inside vault)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            vault_id = data.get("vault_id")
            if vault_id:
                return vault_id
        except (json.JSONDecodeError, OSError):
            pass

    # Migrate from legacy global location if it exists
    legacy = _legacy_vault_identity_path()
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text())
            vault_id = data.get("vault_id")
            if vault_id:
                # Copy to new location
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"vault_id": vault_id, "created": data.get("created", _iso_now()), "migrated_from": str(legacy)}, indent=2))
                os.replace(tmp, path)
                return vault_id
        except (json.JSONDecodeError, OSError):
            pass

    vault_id = uuid.uuid4().hex
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"vault_id": vault_id, "created": _iso_now()}, indent=2))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        # Re-read from disk if it exists (another process may have created it)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return data["vault_id"]
            except (json.JSONDecodeError, KeyError, OSError):
                pass
        return None
    return vault_id


def _iso_now():
    """Current time in ISO 8601."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Runtime directory (private temp files) ---


def _runtime_dir_is_usable(path):
    """Return True when a runtime directory is writable by this process."""
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        probe = os.path.join(path, ".memento-write-test")
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        os.unlink(probe)
        return True
    except OSError:
        return False


def get_runtime_dir():
    """Get a user-private directory for temp files.

    Uses $XDG_RUNTIME_DIR (typically /run/user/$UID, mode 0700) with
    fallback to ~/.cache/memento-vault/. If neither location is writable,
    falls back to a per-user temp dir with mode 0700.
    """
    candidates = []
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        candidates.append(os.path.join(runtime, "memento-vault"))
    candidates.append(os.path.join(str(Path.home()), ".cache", "memento-vault"))
    candidates.append(os.path.join(tempfile.gettempdir(), f"memento-vault-{os.getuid()}"))

    for candidate in candidates:
        if _runtime_dir_is_usable(candidate):
            return candidate

    raise OSError("No writable runtime directory available for memento-vault")


RUNTIME_DIR = get_runtime_dir()


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
