"""QMD search, PRF expansion, RRF fusion, and retrieval enhancements."""

import json
import math
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from memento.config import RUNTIME_DIR, get_config, get_vault
from memento.store import log_retrieval
from memento.graph import (
    apply_pagerank_boost,
    extract_wikilinks,
    load_or_build_graph,
    ppr_expand,
    read_note_metadata,
)

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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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

            results.append(
                {
                    "path": raw_path,
                    "title": title,
                    "score": score,
                    "snippet": _clean_snippet(item.get("snippet", item.get("content", ""))),
                }
            )

        return results[:limit]

    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return []
    except Exception as exc:
        log_retrieval("search", "qmd_search_unexpected", error=str(exc))
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
            query,
            collection=config["qmd_collection"],
            limit=limit,
            semantic=semantic,
            timeout=timeout,
            min_score=min_score,
        )
        return results[:limit]

    # Run primary + extras in parallel
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
    """
    scores = {}  # path -> cumulative RRF score
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


# --- QMD get ---


def qmd_get(path, collection=None, timeout=5):
    """Fetch a single note by path via qmd get.

    Args:
        path: note path relative to collection (e.g. "notes/foo.md")
        collection: QMD collection name (default: from config)
        timeout: subprocess timeout in seconds

    Returns:
        dict with path, title, content keys, or None if not found.
    """
    config = get_config()
    collection = collection or config.get("qmd_collection", "memento")

    if not has_qmd():
        return None

    cmd = ["qmd", "get", path, "-c", collection, "--json"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None

        stdout = result.stdout
        json_start = stdout.find("{")
        if json_start == -1:
            return None

        data = json.loads(stdout[json_start:])
        raw_path = data.get("file", data.get("path", path))
        if "://" in raw_path:
            raw_path = raw_path.split("://", 1)[1]
            parts = raw_path.split("/", 1)
            if len(parts) > 1:
                raw_path = parts[1]

        return {
            "path": raw_path,
            "title": data.get("title", Path(raw_path).stem),
            "content": data.get("content", ""),
            "score": 0.0,
        }

    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None
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


# --- Retrieval enhancements ---


def apply_temporal_decay(results, config=None):
    """Apply temporal decay to search results based on note age and certainty.

    High-certainty notes (>= certainty_floor) are immune to decay.
    Others decay exponentially with a configurable half-life.

    Modifies results in-place and re-sorts by adjusted score.
    """
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
    except ImportError:
        pass  # networkx unavailable
    except Exception as exc:
        log_retrieval("search", "graph_load_failed", error=str(exc))

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
