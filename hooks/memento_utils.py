#!/usr/bin/env python3
"""
Shared utilities for memento-vault hooks.
Config loading, project detection, QMD queries.
"""

import json
import os
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
    "recall_high_confidence": 0.55,  # BM25 score above this skips PRF/RRF/CE
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
    "temporal_decay_half_life": 90,  # days
    "temporal_decay_certainty_floor": 4,  # certainty >= this: no decay
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
    """Search primary collection + any extra_qmd_collections in parallel.

    Returns combined results sorted by score descending.
    """
    config = get_config()
    extras = config.get("extra_qmd_collections", [])

    if not extras:
        # No extra collections — skip threading overhead
        results = qmd_search(
            query, collection=config["qmd_collection"],
            limit=limit, semantic=semantic, timeout=timeout, min_score=min_score,
        )
        return results[:limit]

    # Run primary + extras in parallel
    from concurrent.futures import ThreadPoolExecutor, as_completed

    futures = {}
    with ThreadPoolExecutor(max_workers=len(extras) + 1) as pool:
        futures[pool.submit(
            qmd_search, query, config["qmd_collection"],
            limit, semantic, timeout, min_score,
        )] = "primary"

        for extra in extras:
            futures[pool.submit(
                qmd_search, query, extra,
                max(3, limit // 2), semantic, timeout, min_score,
            )] = extra

        results = []
        for future in as_completed(futures):
            try:
                results.extend(future.result())
            except Exception:
                pass

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


# --- PRF query expansion ---

_STOPWORDS = frozenset((
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "shall", "to", "of",
    "in", "for", "on", "at", "by", "with", "from", "as", "it",
    "its", "this", "that", "these", "those", "which", "who", "whom",
    "what", "when", "where", "how", "not", "no", "and", "or", "but",
    "if", "than", "then", "so", "very",
))


def _extract_expansion_terms(results, original_query, max_terms=5):
    """Extract discriminative terms from search results for query expansion.

    Tokenizes snippets and titles, filters stopwords and original query terms,
    returns top terms by frequency.
    """
    if not results:
        return []

    query_terms = frozenset(original_query.lower().split())

    # Count term frequencies across all results
    freq = {}
    for r in results:
        text = (r.get("snippet", "") + " " + r.get("title", "")).lower()
        # Strip punctuation, split into words
        words = re.findall(r"[a-z0-9]+", text)
        for w in words:
            if len(w) < 3:
                continue
            if w in query_terms:
                continue
            if w in _STOPWORDS:
                continue
            freq[w] = freq.get(w, 0) + 1

    # Sort by frequency descending, take top max_terms
    ranked = sorted(freq, key=lambda t: freq[t], reverse=True)
    return ranked[:max_terms]


def prf_expand_query(query, collection=None, config=None, initial_results=None):
    """Expand a query using Pseudo-Relevance Feedback.

    Extracts top terms from initial search results and appends them
    to the original query. Pass initial_results to avoid a redundant
    BM25 call when you already have results from a prior search.

    Returns the expanded query string, or the original if PRF is
    disabled or no results are found.
    """
    if config is None:
        config = get_config()

    if not config.get("prf_enabled", True):
        return query

    top_docs = config.get("prf_top_docs", 3)
    max_terms = config.get("prf_max_terms", 5)

    results = initial_results[:top_docs] if initial_results else qmd_search(query, collection, limit=top_docs, timeout=3)
    if not results:
        return query

    terms = _extract_expansion_terms(results, query, max_terms=max_terms)
    if not terms:
        return query

    return query + " " + " ".join(terms)


# --- RRF hybrid search ---

VSEARCH_WARM_PATH = "/tmp/memento-vsearch-warm"


def rrf_fuse(result_lists, k=60):
    """Fuse multiple ranked result lists using Reciprocal Rank Fusion.

    Each result list is a list of dicts with at least "path" and "score".
    Returns a single merged list sorted by RRF score descending,
    with scores normalized to 0-1.
    """
    scores = {}      # path -> cumulative RRF score
    best_entry = {}  # path -> dict from highest-scored occurrence

    for result_list in result_lists:
        for rank, item in enumerate(result_list, start=1):
            path = item.get("path", "")
            if not path:
                continue
            rrf_score = 1.0 / (k + rank)
            scores[path] = scores.get(path, 0.0) + rrf_score

            # Keep metadata from the occurrence with the highest original score
            prev = best_entry.get(path)
            if prev is None or item.get("score", 0) > prev.get("score", 0):
                best_entry[path] = dict(item)

    if not scores:
        return []

    max_score = max(scores.values())

    merged = []
    for path, rrf_score in scores.items():
        entry = best_entry[path]
        entry["score"] = rrf_score / max_score if max_score > 0 else 0.0
        merged.append(entry)

    merged.sort(key=lambda r: r["score"], reverse=True)
    return merged


def is_vsearch_warm():
    """Check whether vsearch has been warmed up (deferred briefing consumed)."""
    return os.path.exists(VSEARCH_WARM_PATH)


def mark_vsearch_warm():
    """Touch the warm flag so subsequent prompts can use RRF hybrid search."""
    try:
        Path(VSEARCH_WARM_PATH).touch()
    except OSError:
        pass


# --- Retrieval enhancements ---


def read_note_metadata(note_name):
    """Read frontmatter metadata and wikilinks from a vault note.

    Args:
        note_name: Note filename stem (e.g., 'some-note') or relative path.

    Returns:
        Dict with: date (str|None), certainty (int|None), type (str|None),
        links (list of wikilink target names).
        Returns None if the note file doesn't exist.
    """
    vault = get_vault()
    # Normalize: accept both 'some-note' and 'notes/some-note.md'
    if note_name.endswith(".md"):
        note_path = vault / note_name
    else:
        note_path = vault / "notes" / f"{note_name}.md"

    if not note_path.exists():
        return None

    date = None
    certainty = None
    note_type = None
    project = None
    links = []

    try:
        with open(note_path) as f:
            in_frontmatter = False
            past_frontmatter = False
            for line in f:
                stripped = line.strip()
                if stripped == "---":
                    if not in_frontmatter and not past_frontmatter:
                        in_frontmatter = True
                        continue
                    elif in_frontmatter:
                        in_frontmatter = False
                        past_frontmatter = True
                        continue
                if in_frontmatter:
                    if stripped.startswith("date:"):
                        date = stripped[5:].strip().strip('"').strip("'")
                    elif stripped.startswith("certainty:"):
                        try:
                            certainty = int(stripped[10:].strip())
                        except ValueError:
                            pass
                    elif stripped.startswith("type:"):
                        note_type = stripped[5:].strip()
                    elif stripped.startswith("project:"):
                        project = stripped[8:].strip().strip('"').strip("'")
                if past_frontmatter:
                    # Extract wikilinks from body
                    for match in re.finditer(r"\[\[([^\]]+)\]\]", line):
                        links.append(match.group(1))
    except OSError:
        return None

    return {"date": date, "certainty": certainty, "type": note_type, "project": project, "links": links}


def note_is_superseded(note_name):
    """Check if a note has been superseded by a newer note.

    Scans all notes in the vault for a `supersedes` frontmatter field
    that references this note. Returns the superseding note name if found,
    or None.

    Args:
        note_name: Note filename stem (e.g., 'redis-cache-ttl').
    """
    vault = get_vault()
    notes_dir = vault / "notes"
    if not notes_dir.exists():
        return None

    target = f"[[{note_name}]]"
    for note_path in notes_dir.glob("*.md"):
        if note_path.stem == note_name:
            continue
        try:
            with open(note_path) as f:
                in_frontmatter = False
                for line in f:
                    stripped = line.strip()
                    if stripped == "---":
                        if not in_frontmatter:
                            in_frontmatter = True
                            continue
                        else:
                            break  # end of frontmatter
                    if in_frontmatter and stripped.startswith("supersedes:"):
                        if target in stripped:
                            return note_path.stem
        except OSError:
            continue

    return None


def apply_temporal_decay(results, config=None):
    """Apply temporal decay to search results based on note age and certainty.

    High-certainty notes (>= certainty_floor) are immune to decay.
    Others decay exponentially with a configurable half-life.

    Modifies results in-place and re-sorts by adjusted score.
    """
    import math
    from datetime import datetime

    if config is None:
        config = get_config()

    if not config.get("temporal_decay", True):
        return results

    half_life = config.get("temporal_decay_half_life", 90)
    certainty_floor = config.get("temporal_decay_certainty_floor", 4)
    decay_lambda = math.log(2) / max(half_life, 1)

    now = datetime.now()

    for result in results:
        path = result.get("path", "")
        # Derive note name from path
        note_name = Path(path).stem if path else ""
        if not note_name:
            continue

        meta = read_note_metadata(note_name)
        if meta is None:
            continue

        # Store metadata for later use by wikilink expansion
        result["_meta"] = meta

        certainty = meta.get("certainty")
        if certainty is not None and certainty >= certainty_floor:
            continue  # No decay for high-certainty notes

        date_str = meta.get("date")
        if not date_str:
            continue

        try:
            # Parse ISO date (with or without time)
            note_date = datetime.fromisoformat(date_str)
            age_days = (now - note_date).days
            if age_days <= 0:
                continue

            # Slower decay for certainty 3
            effective_lambda = decay_lambda
            if certainty == 3:
                effective_lambda = decay_lambda / 2

            decay_factor = math.exp(-effective_lambda * age_days)
            result["_original_score"] = result["score"]
            result["score"] = result["score"] * decay_factor
        except (ValueError, TypeError):
            continue

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def expand_wikilinks(results, config=None):
    """Expand search results with wikilinked notes (1 hop).

    For each result that has wikilinks, add linked notes as lower-scored
    entries. Deduplicates against existing results.

    Returns a new list with expanded results.
    """
    if config is None:
        config = get_config()

    if not config.get("wikilink_expansion", True):
        return results

    score_factor = config.get("wikilink_score_factor", 0.5)
    max_hops = config.get("wikilink_max_hops", 1)

    if max_hops < 1:
        return results

    # Track existing paths to avoid duplicates
    seen_paths = set()
    for r in results:
        path = r.get("path", "")
        seen_paths.add(path)
        # Also add by note name for matching
        seen_paths.add(Path(path).stem if path else "")

    max_expanded = config.get("wikilink_max_expanded", 3)
    expanded = []

    for result in results:
        if len(expanded) >= max_expanded:
            break

        # Use cached metadata if available (from temporal_decay), otherwise read
        meta = result.get("_meta")
        if meta is None:
            note_name = Path(result.get("path", "")).stem
            if note_name:
                meta = read_note_metadata(note_name)

        if not meta or not meta.get("links"):
            continue

        parent_score = result.get("_original_score", result.get("score", 0))

        for link_name in meta["links"]:
            if len(expanded) >= max_expanded:
                break

            if link_name in seen_paths:
                continue

            link_meta = read_note_metadata(link_name)
            if link_meta is None:
                continue

            seen_paths.add(link_name)
            link_path = f"notes/{link_name}.md"
            seen_paths.add(link_path)

            expanded.append({
                "path": link_path,
                "title": link_name,
                "score": parent_score * score_factor,
                "snippet": "",
                "_meta": link_meta,
                "_hop": 1,
            })

    # Merge and sort
    all_results = results + expanded
    all_results.sort(key=lambda r: r["score"], reverse=True)
    return all_results


def filter_by_project(results, cwd):
    """Filter results to notes matching the current project.

    Notes with a `project` field that doesn't match cwd are excluded.
    Notes without a `project` field (general knowledge) pass through.
    """
    if not cwd:
        return results

    # Normalize cwd: resolve symlinks, strip trailing slash
    try:
        cwd = os.path.realpath(cwd).rstrip("/")
    except (OSError, ValueError):
        return results

    filtered = []
    for r in results:
        meta = r.get("_meta")
        if meta is None:
            note_name = Path(r.get("path", "")).stem
            if note_name:
                meta = read_note_metadata(note_name)
                r["_meta"] = meta

        if meta is None:
            filtered.append(r)  # Can't read metadata — keep it
            continue

        note_project = meta.get("project")
        if not note_project:
            filtered.append(r)  # No project field — general knowledge
            continue

        # Match if cwd starts with (or equals) the note's project path
        try:
            note_project = os.path.realpath(note_project).rstrip("/")
        except (OSError, ValueError):
            pass

        if cwd.startswith(note_project) or note_project.startswith(cwd):
            filtered.append(r)

    return filtered


def enhance_results(results, config=None, cwd=None):
    """Apply all retrieval enhancements to search results.

    Pipeline order:
      1. Temporal decay (age-based score adjustment)
      2. PageRank boost (centrality-based score boost)
      3. Project filter (scope to current project)
      4. PPR expansion (Personalized PageRank link traversal)
         Falls back to naive wikilink expansion if networkx unavailable.

    Call this after qmd_search to improve result quality.
    Pass cwd to filter out notes from unrelated projects.
    """
    if config is None:
        config = get_config()

    results = apply_temporal_decay(results, config)

    # PageRank boost + PPR expansion (requires networkx + graph)
    graph = None
    pagerank = None
    try:
        vault = get_vault()
        graph, pagerank = load_or_build_graph(vault)
    except Exception:
        pass  # networkx unavailable or graph build failed

    if pagerank:
        results = apply_pagerank_boost(results, pagerank, config)

    if cwd:
        results = filter_by_project(results, cwd)

    # PPR expansion replaces naive wikilink expansion when graph is available
    if graph and config.get("ppr_enabled", True):
        expanded = ppr_expand(results, graph, config)
        if expanded:
            # Merge PPR results, dedup by path
            existing_paths = {r.get("path", "") for r in results}
            for entry in expanded:
                if entry["path"] not in existing_paths:
                    results.append(entry)
                    existing_paths.add(entry["path"])
            results.sort(key=lambda r: r["score"], reverse=True)
    else:
        results = expand_wikilinks(results, config)

    # Clean internal metadata before returning
    for r in results:
        r.pop("_meta", None)
        r.pop("_original_score", None)
        r.pop("_hop", None)
        r.pop("_ppr", None)

    return results


# --- Project retrieval maps ---

_PROJECT_MAPS = None


def load_project_maps(config_dir=None):
    """Load project maps from disk, with module-level caching.

    Returns the "maps" dict: {slug: [{stem, title, certainty, date}, ...]}.
    Returns empty dict if file doesn't exist.
    """
    global _PROJECT_MAPS
    if _PROJECT_MAPS is not None and config_dir is None:
        return _PROJECT_MAPS

    if config_dir is None:
        config_dir = os.path.join(
            os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
            "memento-vault",
        )

    path = Path(config_dir) / "project-maps.json"
    if not path.exists():
        return {}

    try:
        with open(path) as f:
            data = json.load(f)
        maps = data.get("maps", {})
        if config_dir is None:
            _PROJECT_MAPS = maps
        return maps
    except (json.JSONDecodeError, OSError):
        return {}


def lookup_project_notes(project_slug, maps=None, limit=5):
    """Look up notes for a project slug.

    If exact slug not found, tries partial matching (substring in either
    direction).

    Returns list of dicts: [{path, title, score}, ...] capped at limit.
    Score is certainty / 5 (normalized to 0-1).
    """
    if maps is None:
        maps = load_project_maps()

    # Exact match
    entries = maps.get(project_slug)

    # Partial match fallback
    if entries is None:
        for key in maps:
            if project_slug in key or key in project_slug:
                entries = maps[key]
                break

    if not entries:
        return []

    results = []
    for entry in entries[:limit]:
        certainty = entry.get("certainty", 2)
        results.append({
            "path": f"notes/{entry['stem']}.md",
            "title": entry.get("title", entry["stem"]),
            "score": certainty / 5.0,
        })

    return results


# --- Wikilink graph and PageRank ---

try:
    import networkx as nx
    _HAS_NETWORKX = True
except ImportError:
    nx = None
    _HAS_NETWORKX = False

_GRAPH_CACHE = [None]  # mutable container for in-process caching
_GRAPH_CACHE_PATH = "/tmp/memento-wikilink-graph.json"
_GRAPH_CACHE_MAX_AGE = 3600  # 1 hour


def build_wikilink_graph(vault_path):
    """Build a directed graph from wikilinks in vault notes.

    Scans notes/*.md, extracts [[wikilinks]] from the body, and creates
    edges only where the target note exists in the vault.

    Returns:
        nx.DiGraph with note stems as nodes, wikilinks as directed edges.
        Empty DiGraph if networkx is unavailable.
    """
    if not _HAS_NETWORKX:
        return type("FakeGraph", (), {
            "nodes": [], "edges": [],
            "number_of_nodes": lambda s: 0,
            "number_of_edges": lambda s: 0,
        })()

    graph = nx.DiGraph()
    vault = Path(vault_path)
    notes_dir = vault / "notes"

    if not notes_dir.is_dir():
        return graph

    # Collect all note stems that exist
    existing_stems = set()
    for md_file in notes_dir.glob("*.md"):
        existing_stems.add(md_file.stem)

    # Build edges
    wikilink_re = re.compile(r"\[\[([^\]]+)\]\]")

    for md_file in notes_dir.glob("*.md"):
        stem = md_file.stem
        graph.add_node(stem)

        try:
            with open(md_file) as f:
                in_frontmatter = False
                past_frontmatter = False
                for line in f:
                    stripped = line.strip()
                    if stripped == "---":
                        if not in_frontmatter and not past_frontmatter:
                            in_frontmatter = True
                            continue
                        elif in_frontmatter:
                            in_frontmatter = False
                            past_frontmatter = True
                            continue
                    if in_frontmatter:
                        continue
                    if past_frontmatter:
                        for match in wikilink_re.finditer(line):
                            target = match.group(1)
                            if target in existing_stems and target != stem:
                                graph.add_edge(stem, target)
        except OSError:
            continue

    return graph


def compute_pagerank(graph, alpha=0.85):
    """Compute PageRank scores for the wikilink graph.

    Returns:
        Dict mapping stem -> float pagerank score. Empty dict for empty graph.
    """
    if not _HAS_NETWORKX:
        return {}
    if graph.number_of_nodes() == 0:
        return {}
    return dict(nx.pagerank(graph, alpha=alpha))


def _serialize_graph(graph, pagerank, cache_path):
    """Write graph edges and pagerank scores to a JSON cache file."""
    data = {
        "edges": list(graph.edges()),
        "nodes": list(graph.nodes()),
        "pagerank": pagerank,
    }
    tmp = cache_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, cache_path)


def _deserialize_graph(cache_path):
    """Read graph and pagerank from a JSON cache file.

    Returns:
        Tuple of (nx.DiGraph, dict) with edges and pagerank scores.
    """
    with open(cache_path) as f:
        data = json.load(f)

    graph = nx.DiGraph()
    for node in data.get("nodes", []):
        graph.add_node(node)
    for src, dst in data.get("edges", []):
        graph.add_edge(src, dst)

    pagerank = {k: float(v) for k, v in data.get("pagerank", {}).items()}
    return graph, pagerank


def load_or_build_graph(vault_path=None, cache_path=None):
    """Load the wikilink graph from cache or build it fresh.

    Uses a two-level cache: in-process (_GRAPH_CACHE) and on-disk (JSON).
    Disk cache expires after 1 hour.

    Args:
        vault_path: Override vault path (default: from config).
        cache_path: Override cache file path (default: /tmp/memento-wikilink-graph.json).

    Returns:
        Tuple of (nx.DiGraph, dict) with the graph and pagerank scores.
    """
    import time as _time

    if not _HAS_NETWORKX:
        return None, {}

    cache_file = cache_path or _GRAPH_CACHE_PATH

    # Check in-process cache
    if _GRAPH_CACHE[0] is not None:
        return _GRAPH_CACHE[0]

    # Check disk cache
    try:
        age = _time.time() - os.path.getmtime(cache_file)
        if age < _GRAPH_CACHE_MAX_AGE:
            graph, pagerank = _deserialize_graph(cache_file)
            _GRAPH_CACHE[0] = (graph, pagerank)
            return graph, pagerank
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    # Build fresh
    if vault_path is None:
        vault_path = str(get_vault())

    graph = build_wikilink_graph(vault_path)
    config = get_config()
    alpha = config.get("pagerank_alpha", 0.85)
    pagerank = compute_pagerank(graph, alpha=alpha)

    # Write to disk cache
    try:
        _serialize_graph(graph, pagerank, cache_file)
    except OSError:
        pass

    _GRAPH_CACHE[0] = (graph, pagerank)
    return graph, pagerank


def apply_pagerank_boost(results, pagerank, config=None):
    """Boost search result scores using PageRank centrality.

    Well-connected notes get a multiplicative bump so they rank higher
    when BM25/vector scores are close.

    Modifies results in-place and re-sorts by adjusted score.
    """
    if config is None:
        config = get_config()

    weight = config.get("pagerank_boost_weight", 0.3)

    for r in results:
        stem = Path(r["path"]).stem
        pr_score = pagerank.get(stem, 0.0)
        r["score"] *= (1 + weight * pr_score)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def ppr_expand(results, graph, config=None):
    """Expand search results using Personalized PageRank on the wikilink graph.

    Seeds are the note stems from *results*, weighted by their search score.
    PPR propagates relevance through the graph, surfacing structurally
    important notes 2+ hops away that naive 1-hop expansion would miss.

    Args:
        results: List of dicts with at least "path" and "score" keys.
        graph: nx.DiGraph with note stems as nodes.
        config: Optional config dict (keys: ppr_alpha, ppr_max_expanded,
                ppr_min_score).

    Returns:
        List of expanded entries (dicts with path, title, score, _ppr keys),
        sorted by PPR score descending. Empty list on empty graph or if
        networkx is unavailable.
    """
    if not _HAS_NETWORKX:
        return []

    if graph is None or graph.number_of_nodes() == 0:
        return []

    if config is None:
        config = {}

    alpha = config.get("ppr_alpha", 0.85)
    max_expanded = config.get("ppr_max_expanded", 5)
    min_score = config.get("ppr_min_score", 0.01)

    # Build personalization vector: seed stems weighted by search score
    seed_stems = set()
    personalization = {}
    for r in results:
        stem = Path(r.get("path", "")).stem
        if stem and stem in graph:
            seed_stems.add(stem)
            personalization[stem] = r.get("score", 1.0)

    if not personalization:
        return []

    # Run Personalized PageRank
    try:
        ppr = nx.pagerank(graph, alpha=alpha, personalization=personalization)
    except nx.NetworkXError:
        return []

    # Collect non-seed nodes, sorted by PPR score descending
    candidates = [
        (stem, score)
        for stem, score in ppr.items()
        if stem not in seed_stems and score >= min_score
    ]
    candidates.sort(key=lambda x: x[1], reverse=True)

    expanded = []
    for stem, score in candidates[:max_expanded]:
        expanded.append({
            "path": f"notes/{stem}.md",
            "title": stem,
            "score": float(score),
            "_ppr": True,
        })

    return expanded


# --- Concept index (Tenet) ---

_CONCEPT_INDEX = None

CONCEPT_INDEX_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
    "memento-vault", "concept-index.json",
)


def load_concept_index(config_dir=None):
    """Load the concept index from disk. Caches after first load.

    Returns the "index" field: {keyword: [{stem, title, score}, ...]}
    Returns empty dict if file doesn't exist.
    """
    global _CONCEPT_INDEX
    if _CONCEPT_INDEX is not None and config_dir is None:
        return _CONCEPT_INDEX

    if config_dir is not None:
        path = os.path.join(config_dir, "concept-index.json")
    else:
        path = CONCEPT_INDEX_PATH

    try:
        with open(path) as f:
            data = json.load(f)
        index = data.get("index", {})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        index = {}

    # Only cache when using the default path
    if config_dir is None:
        _CONCEPT_INDEX = index

    return index


def lookup_concepts(query, index=None):
    """Look up concept index entries matching query keywords.

    Tokenizes the query, looks up each word in the index, and merges
    results — summing scores when a stem appears from multiple keywords.

    Returns list of dicts sorted by score descending: [{path, title, score}]
    Limited to 5 results.
    """
    if index is None:
        index = load_concept_index()

    if not index or not query:
        return []

    # Tokenize query: lowercase, strip punctuation, drop short words
    words = re.sub(r"[^a-zA-Z0-9\s-]", "", query.lower()).split()
    words = [w for w in words if len(w) >= 3]

    # Merge: stem -> {title, score (summed)}
    merged = {}  # stem -> {"title": str, "score": float}
    for word in words:
        entries = index.get(word, [])
        for entry in entries:
            stem = entry["stem"]
            if stem in merged:
                merged[stem]["score"] += entry["score"]
            else:
                merged[stem] = {"title": entry["title"], "score": entry["score"]}

    if not merged:
        return []

    results = [
        {"path": f"notes/{stem}.md", "title": info["title"], "score": info["score"]}
        for stem, info in merged.items()
    ]
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:5]


# --- Retrieval logging ---

RETRIEVAL_LOG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
    "memento-vault", "retrieval.jsonl",
)


def _should_log():
    """Check if retrieval logging is enabled (config or env var)."""
    if os.environ.get("MEMENTO_DEBUG"):
        return True
    return get_config().get("retrieval_log", False)


def log_retrieval(hook, action, **kwargs):
    """Append a structured log entry to the retrieval log.

    Only writes when retrieval_log is enabled in config or MEMENTO_DEBUG is set.

    Args:
        hook: Hook name (briefing, recall, tool-context)
        action: What happened (search, cache-hit, skip, inject, filter)
        **kwargs: Additional fields (query, results_before, results_after,
                  injected_titles, injected_chars, latency_ms, session_id, cwd)
    """
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


# --- Hook I/O helpers ---


def read_hook_input():
    """Read JSON from stdin (hook event data)."""
    raw = sys.stdin.read()
    return json.loads(raw)


# --- Inception (state management) ---

INCEPTION_STATE_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
    "memento-vault", "inception-state.json",
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
        # Corrupt — rename to .bak and start fresh
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

INCEPTION_LOCK_PATH = "/tmp/memento-inception.lock"


def acquire_inception_lock(lock_path=None):
    """File-based lock for Inception. Returns True if acquired.

    Stale locks older than 10 minutes are broken.
    """
    import time as _time
    path = Path(lock_path or INCEPTION_LOCK_PATH)
    if path.exists():
        try:
            age = _time.time() - path.stat().st_mtime
            if age < 600:  # 10 minutes
                # Check if PID is still alive
                try:
                    pid = int(path.read_text().strip())
                    os.kill(pid, 0)  # signal 0 = check existence
                    return False  # process alive, lock valid
                except (ValueError, OSError):
                    pass  # PID invalid or dead, break lock
        except OSError:
            pass  # stat failed, break lock
    path.write_text(str(os.getpid()))
    return True


def release_inception_lock(lock_path=None):
    """Release the Inception lock file."""
    path = Path(lock_path or INCEPTION_LOCK_PATH)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
