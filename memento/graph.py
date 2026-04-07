"""Wikilink graph, PageRank, concept index, and project maps."""

import json
import os
import re
from pathlib import Path

from memento.config import RUNTIME_DIR, get_config, get_vault

# --- Note metadata ---


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
        return type(
            "FakeGraph",
            (),
            {
                "nodes": [],
                "edges": [],
                "number_of_nodes": lambda s: 0,
                "number_of_edges": lambda s: 0,
            },
        )()

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
