"""Wikilink graph, PageRank, concept index, and project maps."""

import json
import os
import re
from pathlib import Path

from memento import frontmatter
from memento.config import RUNTIME_DIR, get_config, get_vault

# --- Note metadata ---


def read_note_metadata(note_name):
    """Read frontmatter metadata and wikilinks from a vault note.

    Args:
        note_name: Note filename stem (e.g., 'some-note') or relative path.

    Returns:
        Dict with: title (str|None), date (str|None), certainty (int|None),
        type (str|None), source/origin/supersedes/project metadata, tags
        (list of str), and links (list of wikilink target names). Returns
        None if the note file doesn't exist.

    MEM-166: parses frontmatter via :mod:`memento.frontmatter` instead of a
    private line-prefix scanner. The one behavior change from the prior
    implementation: ``tags`` written in block style (``tags:\n  - a\n  -
    b``) are now visible here, same as the inline ``tags: [a, b]`` form --
    previously only the inline form was understood, so block-style tags were
    silently invisible to every caller of this function (search ranking,
    quality signals, contradiction scanning, lifecycle project lookups).
    """
    vault = get_vault()
    # Normalize: accept both 'some-note' and 'notes/some-note.md'
    if note_name.endswith(".md"):
        note_path = vault / note_name
    else:
        note_path = vault / "notes" / f"{note_name}.md"

    if not note_path.exists():
        return None

    try:
        text = note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    fields, body = frontmatter.parse(text)

    def _scalar(key):
        value = fields.get(key)
        return value if isinstance(value, str) else None

    certainty = None
    raw_certainty = fields.get("certainty")
    if isinstance(raw_certainty, str) and raw_certainty:
        try:
            certainty = int(raw_certainty)
        except ValueError:
            certainty = None

    tags = fields.get("tags")
    tags = tags if isinstance(tags, list) else []

    links = [match.group(1) for match in re.finditer(r"\[\[([^\]]+)\]\]", body)]

    return {
        "title": _scalar("title"),
        "date": _scalar("date"),
        "certainty": certainty,
        "type": _scalar("type"),
        "project": _scalar("project"),
        "source": _scalar("source"),
        "origin": _scalar("origin"),
        "supersedes": _scalar("supersedes"),
        "tags": tags,
        "links": links,
    }


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
    target_slug = note_name.strip().strip('"').strip("'")
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
                        value = stripped[len("supersedes:") :].strip().strip('"').strip("'")
                        if target in stripped or value == target_slug or value == f"notes/{target_slug}.md":
                            return note_path.stem
        except OSError:
            continue

    return None


# --- Wikilink extraction ---

_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


def extract_wikilinks(text):
    """Extract [[wikilink]] targets from markdown text.

    Handles [[slug]] and [[slug|alias]] syntax. Ignores links inside
    code blocks. Deduplicates while preserving order. Normalizes spaces
    to hyphens.

    Returns:
        list of slug strings (e.g. ["redis-config", "note-b"])
    """
    if not text:
        return []

    # Strip code blocks to avoid false matches
    cleaned = _CODE_BLOCK_RE.sub("", text)

    seen = set()
    slugs = []
    for match in _WIKILINK_RE.finditer(cleaned):
        slug = match.group(1).strip().replace(" ", "-")
        if slug and slug not in seen:
            seen.add(slug)
            slugs.append(slug)

    return slugs


# --- Wikilink graph and PageRank ---

try:
    import networkx as nx

    _HAS_NETWORKX = True
except ImportError:
    nx = None
    _HAS_NETWORKX = False

_GRAPH_CACHE = [None]  # mutable container for in-process caching
_GRAPH_CACHE_PATH = os.path.join(RUNTIME_DIR, "wikilink-graph.json")
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
        return None

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
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
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


def _vault_notes_max_mtime(vault_path):
    """Return the newest mtime among notes/*.md, or 0.0 if none exist.

    A stat-only scan (no file reads), so it stays cheap even at vault sizes
    around ~5k notes -- see test_tenet_graph.py::test_notes_mtime_scan_is_cheap
    for a benchmark assertion backing this claim.
    """
    notes_dir = Path(vault_path) / "notes"
    if not notes_dir.is_dir():
        return 0.0

    max_mtime = 0.0
    for md_file in notes_dir.glob("*.md"):
        try:
            mtime = md_file.stat().st_mtime
        except OSError:
            continue
        if mtime > max_mtime:
            max_mtime = mtime
    return max_mtime


def load_or_build_graph(vault_path=None, cache_path=None):
    """Load the wikilink graph from cache or build it fresh.

    Uses a two-level cache: in-process (_GRAPH_CACHE) and on-disk (JSON).
    The disk cache is only considered fresh when both hold:
      1. its build timestamp is >= the newest note mtime in the vault
         (so a note written after the cache was built invalidates it), and
      2. it is younger than the 1-hour TTL ceiling (belt-and-suspenders
         against clock skew / missed writes).

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

    if vault_path is None:
        vault_path = str(get_vault())

    # Check disk cache
    try:
        cache_mtime = os.path.getmtime(cache_file)
        age = _time.time() - cache_mtime
        notes_mtime = _vault_notes_max_mtime(vault_path)
        if age < _GRAPH_CACHE_MAX_AGE and cache_mtime >= notes_mtime:
            graph, pagerank = _deserialize_graph(cache_file)
            _GRAPH_CACHE[0] = (graph, pagerank)
            return graph, pagerank
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    # Build fresh
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
        r["score"] *= 1 + weight * pr_score

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
    candidates = [(stem, score) for stem, score in ppr.items() if stem not in seed_stems and score >= min_score]
    candidates.sort(key=lambda x: x[1], reverse=True)

    expanded = []
    for stem, score in candidates[:max_expanded]:
        expanded.append(
            {
                "path": f"notes/{stem}.md",
                "title": stem,
                "score": float(score),
                "_ppr": True,
            }
        )

    return expanded


# --- Related notes (topology only, no scoring) ---

NEIGHBORHOOD_MAX_DEPTH = 3
NEIGHBORHOOD_DEFAULT_CAP = 50


def _related_entry(stem, hop):
    """Build a {stem, title, path, hop} entry using cheap frontmatter metadata."""
    meta = read_note_metadata(stem) or {}
    return {
        "stem": stem,
        "title": meta.get("title"),
        "path": f"notes/{stem}.md",
        "hop": hop,
    }


def resolve_note_reference(note, graph=None):
    """Resolve a stem, relative path, or exact title into a canonical note stem.

    Tries, in order: exact stem match (after stripping a leading "notes/" and
    trailing ".md"), then an exact case-insensitive frontmatter title match.
    Falls back to close-match suggestions (by stem and by title) when nothing
    resolves, so callers can surface a helpful error instead of a bare miss.

    Args:
        note: user-supplied stem, path, or title.
        graph: optional nx.DiGraph (nodes = all note stems) to resolve
            against; when omitted, falls back to scanning notes/*.md.

    Returns:
        Tuple of (stem, suggestions). stem is None when unresolved;
        suggestions is a list of up to 5 close-match stems.
    """
    raw = (note or "").strip()
    if not raw:
        return None, []

    candidate = raw
    if candidate.startswith("notes/"):
        candidate = candidate[len("notes/") :]
    if candidate.endswith(".md"):
        candidate = candidate[: -len(".md")]
    candidate = candidate.strip()

    if graph is not None:
        existing_stems = set(graph.nodes)
    else:
        vault = get_vault()
        notes_dir = vault / "notes"
        existing_stems = {p.stem for p in notes_dir.glob("*.md")} if notes_dir.is_dir() else set()

    if candidate in existing_stems:
        return candidate, []

    title_matches = []
    for stem in existing_stems:
        meta = read_note_metadata(stem) or {}
        title = (meta.get("title") or "").strip()
        if title and title.lower() == raw.lower():
            title_matches.append(stem)

    if len(title_matches) == 1:
        return title_matches[0], []

    import difflib

    close = difflib.get_close_matches(candidate, sorted(existing_stems), n=5, cutoff=0.4)
    suggestions = sorted(set(close) | set(title_matches))[:5]
    return None, suggestions


def graph_neighborhood(graph, stem, direction="both", depth=1, cap=NEIGHBORHOOD_DEFAULT_CAP):
    """BFS outward from *stem*, following outbound/inbound wikilink edges.

    Args:
        graph: nx.DiGraph with note stems as nodes.
        stem: starting note stem (must already be a canonical stem).
        direction: "out" (successors only), "in" (predecessors only), or
            "both". Falls back to "both" for unrecognized values.
        depth: hops to traverse, clamped to [0, NEIGHBORHOOD_MAX_DEPTH].
        cap: maximum number of nodes to return.

    Returns:
        Tuple of (entries, truncated). entries are {stem, title, path, hop}
        dicts in BFS discovery order (excludes the start node itself).
        truncated is True when more nodes exist beyond the cap.
    """
    direction = direction if direction in ("out", "in", "both") else "both"
    try:
        depth = max(0, min(int(depth), NEIGHBORHOOD_MAX_DEPTH))
    except (TypeError, ValueError):
        depth = 1

    entries = []
    truncated = False
    if graph is None or depth == 0 or stem not in graph:
        return entries, truncated

    visited = {stem}
    frontier = [stem]
    for hop in range(1, depth + 1):
        next_frontier = []
        for node in frontier:
            neighbors = []
            if direction in ("out", "both"):
                neighbors.extend(graph.successors(node))
            if direction in ("in", "both"):
                neighbors.extend(graph.predecessors(node))
            for neighbor in neighbors:
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                if len(entries) >= cap:
                    truncated = True
                    continue
                entries.append(_related_entry(neighbor, hop))
                next_frontier.append(neighbor)
        frontier = next_frontier
        if not frontier:
            break

    return entries, truncated


def _parse_supersedes_target(raw):
    """Normalize a raw `supersedes:` frontmatter value down to a bare stem."""
    if not raw:
        return None
    value = str(raw).strip().strip('"').strip("'")
    if value.startswith("[[") and value.endswith("]]"):
        value = value[2:-2]
    if "|" in value:
        value = value.split("|", 1)[0]
    if value.startswith("notes/"):
        value = value[len("notes/") :]
    if value.endswith(".md"):
        value = value[: -len(".md")]
    value = value.strip()
    return value or None


def supersession_chain(stem):
    """Walk `supersedes` edges in both directions from *stem*, oldest to newest.

    A note's own `supersedes` frontmatter points to the older note it
    replaces; `note_is_superseded` finds the newer note (if any) that
    replaced it. Both directions are walked until no further link is found
    or a cycle is detected.

    Returns:
        List of {stem, title, path, hop} entries, oldest first. hop is the
        signed distance from *stem* (negative = older, 0 = stem itself,
        positive = newer).
    """
    seen = {stem}

    older = []
    current = stem
    hop = 0
    while True:
        meta = read_note_metadata(current) or {}
        target = _parse_supersedes_target(meta.get("supersedes"))
        if not target or target in seen:
            break
        seen.add(target)
        hop -= 1
        older.append(_related_entry(target, hop))
        current = target

    newer = []
    current = stem
    hop = 0
    while True:
        next_stem = note_is_superseded(current)
        if not next_stem or next_stem in seen:
            break
        seen.add(next_stem)
        hop += 1
        newer.append(_related_entry(next_stem, hop))
        current = next_stem

    return list(reversed(older)) + [_related_entry(stem, 0)] + newer


def build_related_view(note, direction="both", depth=1, cap=NEIGHBORHOOD_DEFAULT_CAP):
    """Build the full topology view for a note: outbound/inbound links, a BFS
    neighborhood, and its supersession chain.

    Pure topology -- no embedding/scoring work. Reuses load_or_build_graph
    (the same cache the hook recall pipeline uses) instead of building a
    second graph. Returns a structured error dict (never raises) when
    networkx is unavailable or *note* can't be resolved.

    Args:
        note: stem, relative path, or exact title to resolve.
        direction: neighborhood traversal direction: "out", "in", or "both".
        depth: neighborhood BFS depth, clamped to [0, NEIGHBORHOOD_MAX_DEPTH].
        cap: maximum neighborhood nodes to return before truncating.

    Returns:
        Dict with note/path/title, outbound, inbound, neighborhood
        ({"nodes": [...], "truncated": bool}), and supersession_chain. On
        failure: {"error": str, "reason": str, [suggestions: list]}.
    """
    if not _HAS_NETWORKX:
        return {
            "error": "graph features require networkx, which is not installed in this environment",
            "reason": "networkx_unavailable",
        }

    vault = get_vault()
    graph, _pagerank = load_or_build_graph(vault)
    if graph is None:
        return {
            "error": "graph features require networkx, which is not installed in this environment",
            "reason": "networkx_unavailable",
        }

    stem, suggestions = resolve_note_reference(note, graph=graph)
    if stem is None:
        return {
            "error": f"note not found: {note!r}",
            "reason": "note_not_found",
            "suggestions": suggestions,
        }

    normalized_direction = direction if direction in ("out", "in", "both") else "both"
    try:
        clamped_depth = max(0, min(int(depth), NEIGHBORHOOD_MAX_DEPTH))
    except (TypeError, ValueError):
        clamped_depth = 1

    outbound = [_related_entry(target, 1) for target in graph.successors(stem)]
    inbound = [_related_entry(source, 1) for source in graph.predecessors(stem)]
    neighborhood_nodes, truncated = graph_neighborhood(
        graph, stem, direction=normalized_direction, depth=clamped_depth, cap=cap
    )
    chain = supersession_chain(stem)
    self_entry = _related_entry(stem, 0)

    return {
        "note": stem,
        "path": self_entry["path"],
        "title": self_entry["title"],
        "direction": normalized_direction,
        "depth": clamped_depth,
        "outbound": outbound,
        "inbound": inbound,
        "neighborhood": {"nodes": neighborhood_nodes, "truncated": truncated},
        "supersession_chain": chain,
    }


# --- Concept index (Tenet) ---

_CONCEPT_INDEX = None

CONCEPT_INDEX_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
    "memento-vault",
    "concept-index.json",
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
        {"path": f"notes/{stem}.md", "title": info["title"], "score": info["score"]} for stem, info in merged.items()
    ]
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:5]


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
        results.append(
            {
                "path": f"notes/{entry['stem']}.md",
                "title": entry.get("title", entry["stem"]),
                "score": certainty / 5.0,
            }
        )

    return results
