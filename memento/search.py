"""Search, PRF expansion, RRF fusion, and retrieval enhancements."""

import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from memento.config import RUNTIME_DIR, detect_project, get_config, get_vault, repo_slug_from_path
from memento.search_backend import _clean_snippet, get_backend  # noqa: F401 (_clean_snippet re-exported for compat)
from memento.store import apply_access_log_boost, log_retrieval, read_durability_tier
from memento.graph import (
    apply_pagerank_boost,
    extract_wikilinks,
    load_or_build_graph,
    ppr_expand,
    read_note_metadata,
    note_is_superseded,
)


MISS_RECOVERY_HINTS = {
    "no_exact_match": ["Try a broader or narrower query."],
    "no_concrete_match": [
        "Try a broader query or remove exact punctuation.",
        "Call memento_get if the note path is known.",
    ],
    "backend_unavailable": [
        "Check memento_status for search backend health.",
        "Run memento_reindex if the index is stale.",
    ],
    "threshold_too_high": ["Lower min_score."],
    "project_filter_removed_all": ["Remove or change the cwd project filter."],
    "query_too_broad": ["Try a narrower query with concrete terms."],
    "literal_mode_auto_selected": [
        "Try a more descriptive natural-language query.",
        "Call memento_get if the note path is known.",
    ],
    "semantic_mode_not_available": ["Try semantic=false.", "Run memento_reindex if index state looks stale."],
    "empty_vault": ["Capture or sync notes first.", "Run memento_status to verify the vault path."],
    "index_stale_or_missing": ["Run memento_reindex if index state looks stale."],
    "filters_eliminated_all": [
        "Broaden or remove the search filters.",
        "Use memento_query to inspect raw metadata without ranking.",
    ],
}

_REASON_ALIASES = {
    "qmd-unavailable": "backend_unavailable",
    "vault-unavailable": "empty_vault",
    "project-mismatch-filtered-empty": "project_filter_removed_all",
    "filtered-empty": "no_exact_match",
    "no-results": "no_exact_match",
    "broad-project-query": "query_too_broad",
    "skipped-prompt": "query_too_broad",
    "low-signal-prompt": "query_too_broad",
}


def build_search_miss(reason: str, details: Optional[dict] = None, recovery_hints: Optional[list[str]] = None) -> dict:
    """Build structured metadata for a retrieval miss."""
    hints = (
        list(recovery_hints)
        if recovery_hints is not None
        else list(MISS_RECOVERY_HINTS.get(reason, MISS_RECOVERY_HINTS["no_exact_match"]))
    )
    miss = {
        "reason": reason,
        "recovery_hints": hints,
    }
    if details:
        miss["details"] = details
    return miss


def miss_envelope(
    reason: str,
    details: Optional[dict] = None,
    recovery_hints: Optional[list[str]] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Return the structured empty search envelope used by agent-facing surfaces."""
    envelope = {"results": [], "miss": build_search_miss(reason, details=details, recovery_hints=recovery_hints)}
    if metadata:
        envelope["metadata"] = metadata
    return envelope


SEARCH_DETAIL_LEVELS = ("brief", "summary", "full")
_SEARCH_CONTENT_SUFFIX = "\n[vault] content truncated; use memento_get for full note"


def _strip_injection(text: str):
    """Strip instruction-like patterns from content (defense-in-depth)."""
    if not text:
        return text
    text = re.sub(r"(?i)(ignore\s+(all\s+)?previous\s+instructions)", "[filtered]", text)
    text = re.sub(r"(?i)(you\s+are\s+now\s+|you\s+must\s+now\s+)", "[filtered]", text)
    text = re.sub(r"(?i)^(system|assistant)\s*:", "[filtered]:", text, flags=re.MULTILINE)
    text = re.sub(r"</?s>", "", text)
    return text


def normalize_search_detail_level(detail_level: object = "summary") -> str:
    """Normalize search detail levels onto the supported set."""
    if detail_level is None:
        return "summary"
    normalized = str(detail_level).strip().lower()
    return normalized if normalized in SEARCH_DETAIL_LEVELS else "summary"


def _approximate_token_budget(token_budget: Optional[int]) -> Optional[int]:
    if token_budget is None:
        return None
    try:
        budget = max(0, int(token_budget))
    except (TypeError, ValueError):
        return None
    return budget * 4


def _search_result_content(vault: Path, result: dict) -> str:
    content = result.get("content", "")
    if isinstance(content, str) and content.strip():
        return content
    path = str(result.get("path", "")).strip()
    if not path:
        return ""
    try:
        note_path = (vault / path).resolve()
        vault_resolved = vault.resolve()
        if note_path.exists() and note_path != vault_resolved and vault_resolved in note_path.parents:
            return note_path.read_text(errors="replace")
    except (OSError, UnicodeDecodeError, ValueError):
        return ""
    return ""


def shape_search_results(
    results: list[dict],
    *,
    vault: Path,
    detail_level: object = "summary",
    include_content: bool = False,
    token_budget: Optional[int] = 2000,
) -> dict:
    """Shape search results into a token-aware response envelope."""
    normalized_detail = normalize_search_detail_level(detail_level)
    effective_include_content = bool(include_content) or normalized_detail == "full"
    remaining_chars = _approximate_token_budget(token_budget) if effective_include_content else None
    shaped: list[dict] = []
    expandable_paths: list[str] = []
    truncated = False

    for result in results:
        path = _strip_injection(str(result.get("path", "")))
        entry = {
            "path": path,
            "title": _strip_injection(str(result.get("title", ""))),
            "score": round(result.get("score", 0.0), 4),
            # MEM-127: which backend produced this result (qmd | embedded-fts
            # | embedded-vec | grep), so callers can reason about cross
            # -backend score comparability. "unknown" covers results that
            # didn't originate from a backend.search() call (e.g. wikilink
            # -expansion entries fetched via backend.get()).
            "backend": result.get("backend", "unknown"),
        }
        snippet = _strip_injection(str(result.get("snippet", "")).strip())
        if normalized_detail in ("summary", "full") and snippet:
            entry["snippet"] = snippet[:160] if normalized_detail == "summary" else snippet

        if effective_include_content:
            content = _strip_injection(_search_result_content(vault, result).strip())
            if content:
                if remaining_chars is None:
                    entry["content"] = content
                elif remaining_chars <= 0:
                    truncated = True
                    if path:
                        expandable_paths.append(path)
                elif len(content) <= remaining_chars:
                    entry["content"] = content
                    remaining_chars -= len(content)
                else:
                    cutoff = max(0, remaining_chars - len(_SEARCH_CONTENT_SUFFIX))
                    entry["content"] = content[:cutoff].rstrip() + _SEARCH_CONTENT_SUFFIX
                    truncated = True
                    if path:
                        expandable_paths.append(path)
                    remaining_chars = 0
            elif path:
                expandable_paths.append(path)
        elif path:
            expandable_paths.append(path)

        shaped.append(entry)

    metadata = {
        "detail_level": normalized_detail,
        "include_content": effective_include_content,
        "token_budget": token_budget,
        "truncated": truncated,
        "expandable_paths": list(dict.fromkeys(expandable_paths)),
    }
    return {"results": shaped, "metadata": metadata}


def is_literal_like_query(query: str) -> bool:
    """Return True for conservative path/identifier/symbol-looking queries."""
    text = (query or "").strip()
    if not text:
        return False
    if (text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"')):
        return True
    if re.search(r'".+?"', text) or re.search(r"(?<!\w)'.+?'(?!\w)", text):
        return True
    if re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", text, re.I):
        return True
    if re.search(r"[\w.-]+/[\w./-]+", text):
        return True
    if re.search(r"\b[\w.-]+\.(py|ts|tsx|js|jsx|md|json|ya?ml|toml|sh|rb|go|rs)\b", text, re.I):
        return True
    tokens = re.findall(r"[A-Za-z0-9_.:-]+", text)
    if not tokens or len(tokens) > 3:
        return False
    if len(tokens) == 1 and re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", tokens[0]):
        return True
    return any(
        "_" in token or "::" in token or "." in token or re.search(r"[a-z][A-Z]", token) or re.search(r"\d", token)
        for token in tokens
    )


def resolve_concrete_mode(concrete: object = "auto", query: str = "") -> tuple[bool, bool]:
    """Resolve a user concrete option into (enabled, auto_selected)."""
    if concrete is None or concrete == "auto":
        enabled = is_literal_like_query(query)
        return enabled, enabled
    if isinstance(concrete, str):
        normalized = concrete.strip().lower()
        if normalized in ("auto", ""):
            enabled = is_literal_like_query(query)
            return enabled, enabled
        if normalized in ("true", "1", "yes", "on"):
            return True, False
        if normalized in ("false", "0", "no", "off"):
            return False, False
        return False, False
    return bool(concrete), False


def normalize_miss_reason(reason: Optional[str], query: str = "") -> str:
    """Map legacy retrieval skip reasons onto agent-facing miss reason codes."""
    normalized = _REASON_ALIASES.get(reason or "", reason or "no_exact_match")
    if normalized == "no_exact_match" and is_literal_like_query(query):
        return "literal_mode_auto_selected"
    return normalized


# --- Backend-delegating wrappers (backward compat) ---


def has_qmd():
    """Check if the search backend is available."""
    return get_backend().is_available()


def qmd_search(query, collection=None, limit=5, semantic=False, timeout=10, min_score=0.0, concrete=False):
    """Run a search via the configured backend.

    Args:
        query: Search query string
        collection: Collection name (default: from config)
        limit: Max results
        semantic: If True, use vector search; otherwise BM25
        timeout: Backend timeout in seconds
        min_score: Minimum relevance score (0.0-1.0)
        concrete: If True, prefer literal substring/identifier matching.

    Returns:
        List of dicts with keys: path, title, score, snippet
        Empty list if backend unavailable or query fails.
    """
    if not query or not query.strip():
        return []

    config = get_config()
    collection = collection or config["qmd_collection"]

    backend = get_backend()
    if not backend.is_available():
        return []

    try:
        results = backend.search(
            query,
            collection,
            limit=limit,
            semantic=semantic,
            timeout=timeout,
            min_score=min_score,
            concrete=concrete,
        )
    except Exception as exc:
        log_retrieval("search", "qmd_search_unexpected", error=str(exc))
        return []
    # Archived notes are retired from active retrieval: the vault indexes
    # **/*.md, so without this filter archive/ content keeps surfacing in
    # recall and tool-context forever. Explicit get-by-path still works.
    return [r for r in results if not str(r.get("path", "")).startswith("archive/")]


def qmd_search_with_extras(query, limit=5, semantic=False, timeout=5, min_score=0.0, concrete=False):
    """Search primary collection + any extra collections in parallel.

    Returns combined results sorted by score descending.
    """
    config = get_config()
    extras = [] if concrete else config.get("extra_qmd_collections", [])

    if not extras:
        results = qmd_search(
            query,
            collection=config["qmd_collection"],
            limit=limit,
            semantic=semantic,
            timeout=timeout,
            min_score=min_score,
            concrete=concrete,
        )
        return results[:limit]

    from concurrent.futures import ThreadPoolExecutor, as_completed

    futures = {}
    with ThreadPoolExecutor(max_workers=len(extras) + 1) as pool:
        futures[
            pool.submit(
                qmd_search,
                query,
                config["qmd_collection"],
                limit,
                semantic,
                timeout,
                min_score,
                concrete,
            )
        ] = "primary"

        for extra in extras:
            futures[
                pool.submit(
                    qmd_search,
                    query,
                    extra,
                    max(3, limit // 2),
                    semantic,
                    timeout,
                    min_score,
                    concrete,
                )
            ] = extra

        results = []
        for future in as_completed(futures):
            try:
                results.extend(future.result())
            except Exception as exc:
                log_retrieval("search", "extra_collection_failed", error=str(exc))

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


# --- PRF query expansion ---

_STOPWORDS = frozenset(
    (
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "shall",
        "to",
        "of",
        "in",
        "for",
        "on",
        "at",
        "by",
        "with",
        "from",
        "as",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "which",
        "who",
        "whom",
        "what",
        "when",
        "where",
        "how",
        "not",
        "no",
        "and",
        "or",
        "but",
        "if",
        "than",
        "then",
        "so",
        "very",
    )
)


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

    results = (
        initial_results[:top_docs] if initial_results else qmd_search(query, collection, limit=top_docs, timeout=3)
    )
    if not results:
        return query

    terms = _extract_expansion_terms(results, query, max_terms=max_terms)
    if not terms:
        return query

    return query + " " + " ".join(terms)


# --- RRF hybrid search ---

VSEARCH_WARM_PATH = os.path.join(RUNTIME_DIR, "vsearch-warm")


def rrf_fuse(result_lists, k=60):
    """Fuse multiple ranked result lists using Reciprocal Rank Fusion.

    Each result list is a list of dicts with at least "path" and "score".
    Returns a single merged list sorted by RRF score descending,
    with scores normalized to 0-1.

    RRF's own rank-based score is purely positional: a document that is the
    sole candidate in every input list always ranks #1 in each, so
    rrf_score/max_rrf normalizes to 1.0 regardless of how weak that
    document's actual per-backend score was (MEM-143). Post-MEM-127, every
    backend's own "score" is already normalized to a comparable [0, 1]
    scale, so the fused score is capped at the document's own best
    underlying normalized score: `fused = rrf_normalized * best_quality`.
    Rank still decides ORDERING (via rrf_normalized), it just can no longer
    manufacture quality above what the underlying backends actually measured.
    """
    scores = {}  # path -> cumulative RRF score
    best_entry = {}  # path -> dict from highest-scored occurrence
    best_quality = {}  # path -> best underlying normalized backend score

    for result_list in result_lists:
        for rank, item in enumerate(result_list, start=1):
            path = item.get("path", "")
            if not path:
                continue
            rrf_score = 1.0 / (k + rank)
            scores[path] = scores.get(path, 0.0) + rrf_score

            quality = float(item.get("score", 0) or 0)
            if quality > best_quality.get(path, 0.0):
                best_quality[path] = quality

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
        rrf_normalized = rrf_score / max_score if max_score > 0 else 0.0
        entry["score"] = rrf_normalized * best_quality.get(path, 0.0)
        merged.append(entry)

    merged.sort(key=lambda r: r["score"], reverse=True)
    return merged


def is_vsearch_warm():
    """Check whether vsearch has been warmed up (deferred briefing consumed)."""
    return os.path.exists(VSEARCH_WARM_PATH)


def mark_vsearch_warm():
    """Touch the warm flag so subsequent prompts can use RRF hybrid search."""
    try:
        warm_path = Path(VSEARCH_WARM_PATH)
        warm_path.parent.mkdir(parents=True, exist_ok=True)
        warm_path.touch()
    except OSError:
        pass


# --- Backend get ---


def qmd_get(path, collection=None, timeout=5):
    """Fetch a single note by path via the search backend.

    Args:
        path: note path relative to collection (e.g. "notes/foo.md")
        collection: Collection name (default: from config)
        timeout: backend timeout in seconds

    Returns:
        dict with path, title, content keys, or None if not found.
    """
    backend = get_backend()
    if not backend.is_available():
        return None

    try:
        return backend.get(path, collection=collection, timeout=timeout)
    except Exception as exc:
        log_retrieval("search", "qmd_get_unexpected", error=str(exc))
        return None


# --- Multi-hop retrieval ---


def multi_hop_search(query, initial_results, config=None):
    """Follow wikilinks from top results to pull in connected notes.

    Fetches full content of top results, extracts [[wikilinks]], then
    directly fetches linked notes via qmd get. Merges results deduplicated
    by path, sorted by score descending.

    Args:
        query: original user prompt (unused, kept for API compat)
        initial_results: results from the first search pass
        config: dict with multi_hop_max (default 2)

    Returns:
        merged result list, sorted by score descending
    """
    if not initial_results:
        return []

    if config is None:
        config = get_config()

    max_added = config.get("multi_hop_max", 2)
    all_results = list(initial_results)
    seen_paths = {r["path"] for r in all_results}
    # Also track by stem for wikilink matching (links use stem, not full path)
    seen_stems = {Path(r["path"]).stem for r in all_results}

    added = 0
    # Only inspect top 3 results for wikilinks
    for r in initial_results[:3]:
        if added >= max_added:
            break

        # Fetch full note content to extract wikilinks
        note = qmd_get(r["path"])
        if not note:
            continue

        links = extract_wikilinks(note.get("content", ""))
        for slug in links:
            if added >= max_added:
                break
            if slug in seen_stems:
                continue

            # Try to fetch the linked note
            linked = qmd_get(f"notes/{slug}.md")
            if linked and linked["path"] not in seen_paths:
                all_results.append(linked)
                seen_paths.add(linked["path"])
                seen_stems.add(slug)
                added += 1

    all_results.sort(key=lambda r: r["score"], reverse=True)
    return all_results


def expand_result_links(shaped_results, config=None, top_n=3, max_expanded=None):
    """Fetch 1-hop wikilink neighbors of the top shaped search results.

    Thin exposure shim over the same primitives multi_hop_search uses
    (qmd_get + extract_wikilinks), for memento_search's opt-in
    `expand_links` parameter. Unlike multi_hop_search, entries are returned
    separately -- never merged or re-sorted with the direct hits -- so a
    caller can append them after direct results without an expanded note
    ever outranking a direct one.

    Args:
        shaped_results: already-shaped search result dicts (must have "path").
        config: dict with multi_hop_max (default 2); reused as max_expanded
            when max_expanded is not given explicitly.
        top_n: only follow links from this many top results.
        max_expanded: cap on the number of expanded entries returned.

    Returns:
        List of dicts: path, title, score (0.0), snippet, via_link (the
        stem of the direct-hit result the entry was discovered through).
    """
    if not shaped_results:
        return []

    if config is None:
        config = get_config()
    if max_expanded is None:
        max_expanded = config.get("multi_hop_max", 2)

    seen_paths = {r.get("path", "") for r in shaped_results}
    expanded = []
    added = 0

    for result in shaped_results[:top_n]:
        if added >= max_expanded:
            break

        source_path = result.get("path", "")
        source_stem = Path(source_path).stem if source_path else ""

        note = qmd_get(source_path)
        if not note:
            continue

        for slug in extract_wikilinks(note.get("content", "")):
            if added >= max_expanded:
                break

            linked_path = f"notes/{slug}.md"
            if linked_path in seen_paths:
                continue

            linked = qmd_get(linked_path)
            if not linked:
                continue

            seen_paths.add(linked_path)
            expanded.append(
                {
                    "path": linked.get("path", linked_path),
                    "title": linked.get("title", slug),
                    "score": 0.0,
                    "snippet": (linked.get("content", "") or "").strip()[:160],
                    "via_link": source_stem,
                }
            )
            added += 1

    return expanded


# --- Retrieval enhancements ---


def apply_temporal_decay(results, config=None):
    """Apply temporal decay to search results based on note age and durability tier.

    Decay immunity is driven by the derived durability tier (MEM-150), not
    certainty: `pinned` (manual) and `hot` (resurfaced within
    `durability_hot_window_days`) notes are immune. `warm` (resurfaced at
    some point) and `cold` (never resurfaced) notes decay exponentially with
    a configurable half-life regardless of certainty -- a certainty-5 note
    nobody has looked at in 90 days sinks like any other. Certainty still
    slows (but no longer stops) decay at certainty 3, unchanged from before.
    `temporal_decay_certainty_floor` is deprecated and no longer read here.

    Modifies results in-place and re-sorts by adjusted score.
    """
    if config is None:
        config = get_config()

    if not config.get("temporal_decay", True):
        return results

    half_life = config.get("temporal_decay_half_life", 90)
    decay_lambda = math.log(2) / max(half_life, 1)

    now = datetime.now()
    vault = get_vault()

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

        tier = read_durability_tier(vault, path, config=config, now=now) if path else "cold"
        result["_durability_tier"] = tier
        if tier in ("pinned", "hot"):
            continue  # Decay-immune tiers.

        certainty = meta.get("certainty")
        date_str = meta.get("date")
        if not date_str:
            try:
                undated_factor = float(config.get("temporal_decay_undated_factor", 0.5))
            except (TypeError, ValueError):
                undated_factor = 0.5
            result["_original_score"] = result.get("score", 0.0)
            result["score"] = result.get("score", 0.0) * undated_factor
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

            expanded.append(
                {
                    "path": link_path,
                    "title": link_name,
                    "score": parent_score * score_factor,
                    "snippet": "",
                    "_meta": link_meta,
                    "_hop": 1,
                }
            )

    # Merge and sort
    all_results = results + expanded
    all_results.sort(key=lambda r: r["score"], reverse=True)
    return all_results


def filter_by_project(results, cwd, require_match=False):
    """Filter results to notes matching the current project.

    Notes with a `project` field that doesn't match cwd are excluded.
    By default, notes without a `project` field (general knowledge) pass
    through. With require_match=True only positively matched notes survive —
    the bar for unsolicited injection surfaces like tool-context, where
    untagged junk previously slipped through as "general knowledge".

    Comparison is slug-to-slug (MEM-164), not path-prefix: the query cwd is
    resolved to its repo-name slug via ``detect_project`` (same derivation
    used at write time), and each note's `project` field is compared
    case-insensitively against it. Notes already backfilled to a slug compare
    directly; notes still holding a legacy raw path (not yet backfilled) have
    their slug derived on the fly via ``repo_slug_from_path`` so old and new
    notes for the same repo match uniformly.
    """
    if not cwd:
        return results

    try:
        query_slug, _ticket = detect_project(cwd, None)
    except Exception:
        return results

    query_slug = (query_slug or "").strip().lower()
    if not query_slug or query_slug == "unknown":
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
            if require_match:
                log_retrieval("search", "project_match_required", path=r.get("path", ""), reason="no-metadata")
            else:
                filtered.append(r)  # Can't read metadata — keep it
            continue

        note_project = meta.get("project")
        if not note_project:
            if require_match:
                log_retrieval("search", "project_match_required", path=r.get("path", ""), reason="no-project-field")
            else:
                filtered.append(r)  # No project field — general knowledge
            continue

        note_project_raw = str(note_project).strip().strip('"').strip("'")
        if not note_project_raw:
            continue

        if "/" in note_project_raw or "\\" in note_project_raw:
            # Legacy note not yet backfilled to a slug - derive it on the fly.
            note_slug = repo_slug_from_path(note_project_raw) or ""
        else:
            note_slug = note_project_raw

        if note_slug.strip().lower() == query_slug:
            filtered.append(r)
        elif require_match:
            log_retrieval("search", "project_match_required", path=r.get("path", ""), reason="slug-mismatch")

    return filtered


# Paths whose content is operational logging, not curated knowledge: daily
# fleeting logs and project index files. They rank in BM25 because they quote
# session summaries verbatim, but injecting them is noise.
QUALITY_LOG_SHAPED_PREFIXES = ("fleeting/", "projects/")


def _quality_factor(config, key, default):
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def apply_quality_signals(results, config=None):
    """Drop or down-rank low-quality note classes in retrieval candidates.

    Applied in the shared enhancement pipeline (recall, tool-context,
    deferred briefing, MCP search). Classes, per audit 2026-06-10 Part 2:
    - queued Pi raw captures (type: session + pi/queued tags): dropped
    - log-shaped paths (fleeting daily logs, project index files): dropped
    - remaining legacy `type: session` notes: penalized, except old Pi manual
      captures are treated as typed low-certainty discoveries for MEM-50
      compatibility
    - low certainty (<= 2): mild penalty; untyped notes: mild penalty
    """
    if config is None:
        config = get_config()
    if not config.get("quality_signals_enabled", True):
        return results

    session_factor = _quality_factor(config, "quality_session_note_factor", 0.85)
    untyped_factor = _quality_factor(config, "quality_untyped_factor", 0.95)
    low_certainty_factor = _quality_factor(config, "quality_low_certainty_factor", 0.9)
    superseded_factor = _quality_factor(config, "quality_superseded_factor", 0.8)

    kept = []
    for r in results:
        path = str(r.get("path", ""))
        if path.startswith(QUALITY_LOG_SHAPED_PREFIXES):
            log_retrieval("search", "quality_excluded", path=path, reason="log-shaped")
            continue

        meta = r.get("_meta")
        if meta is None:
            note_name = Path(path).stem
            if note_name:
                meta = read_note_metadata(note_name)
                r["_meta"] = meta

        if meta:
            # Frontmatter values may be quoted or cased freely; normalize so
            # `type: "Session"` or `tags: [PI, QUEUED]` can't bypass the rules.
            note_type = str(meta.get("type") or "").strip().strip('"').strip("'").lower()
            tags = {str(tag).strip().strip('"').strip("'").lower() for tag in (meta.get("tags") or [])}
            if note_type == "session" and "queued" in tags:
                log_retrieval("search", "quality_excluded", path=path, reason="queued-session-capture")
                continue
            source = str(meta.get("source") or "").strip().strip('"').strip("'").lower()
            origin = str(meta.get("origin") or "").strip().strip('"').strip("'").lower()
            legacy_pi_capture = note_type == "session" and (
                "pi" in tags or source in {"pi", "pi-capture"} or origin.startswith("pi_bridge")
            )
            factor = 1.0
            if note_type == "session" and not legacy_pi_capture:
                factor *= session_factor
            elif not note_type:
                factor *= untyped_factor
            certainty = meta.get("certainty")
            if certainty is None and legacy_pi_capture:
                certainty = 2
            if certainty is not None and certainty <= 2:
                factor *= low_certainty_factor
            note_name = Path(path).stem if path else ""
            if note_name and note_is_superseded(note_name):
                factor *= superseded_factor
            if factor != 1.0:
                r["score"] = round(r.get("score", 0.0) * factor, 4)
        kept.append(r)

    kept.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    return kept


def enhance_results(results, config=None, cwd=None, require_project_match=False):
    """Apply all retrieval enhancements to search results.

    Pipeline order:
      1. Temporal decay (age-based score adjustment)
      2. Quality signals (drop/penalize low-quality note classes)
      3. PageRank boost (centrality-based score boost)
      4. Access-log boost (recent/frequent successful retrievals)
      5. Project filter (scope to current project)
      6. PPR expansion (Personalized PageRank link traversal)
         Falls back to naive wikilink expansion if networkx unavailable.

    Call this after qmd_search to improve result quality.
    Pass cwd to filter out notes from unrelated projects;
    require_project_match=True demands a positive project match (used by
    tool-context, where notes without project metadata were the junk class).
    """
    if config is None:
        config = get_config()

    results = apply_temporal_decay(results, config)
    results = apply_quality_signals(results, config)

    # PageRank boost + PPR expansion (requires networkx + graph).
    # Both are gated by ppr_enabled: when disabled, skip graph loading
    # entirely so networkx's mere presence in the environment can never
    # change ranking (MEM-138 -- previously the boost below applied
    # unconditionally whenever networkx happened to be importable).
    graph = None
    pagerank = None
    if config.get("ppr_enabled", True):
        try:
            vault = get_vault()
            graph, pagerank = load_or_build_graph(vault)
        except ImportError:
            pass  # networkx unavailable
        except Exception as exc:
            log_retrieval("search", "graph_load_failed", error=str(exc))

    if pagerank:
        results = apply_pagerank_boost(results, pagerank, config)

    results = apply_access_log_boost(results, config)

    if cwd:
        results = filter_by_project(results, cwd, require_match=require_project_match)

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
