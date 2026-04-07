"""Tests for wikilink graph building and PageRank computation."""

import os
import time
from unittest.mock import patch

import pytest

from memento.graph import (
    apply_pagerank_boost,
    compute_pagerank,
    _serialize_graph,
    _deserialize_graph,
    build_wikilink_graph,
    load_or_build_graph,
)


def _write_note(vault, stem, body, frontmatter=None):
    """Helper: write a markdown note with optional frontmatter and body."""
    lines = ["---"]
    fm = frontmatter or {}
    fm.setdefault("title", stem)
    fm.setdefault("type", "discovery")
    fm.setdefault("date", "2026-03-20T10:00")
    fm.setdefault("certainty", 3)
    fm.setdefault("tags", "[graph, test]")
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    lines.append("")
    path = vault / "notes" / f"{stem}.md"
    path.write_text("\n".join(lines))
    return path


# --- build_wikilink_graph ---


def test_build_graph_from_notes(tmp_vault, mock_config):
    """Three notes with known wikilinks produce correct nodes and edges."""
    _write_note(tmp_vault, "alpha", "See [[beta]] and [[gamma]] for details.")
    _write_note(tmp_vault, "beta", "Related to [[alpha]].")
    _write_note(tmp_vault, "gamma", "Standalone note, no links.")

    graph = build_wikilink_graph(str(tmp_vault))

    assert set(graph.nodes) == {"alpha", "beta", "gamma"}
    assert graph.has_edge("alpha", "beta")
    assert graph.has_edge("alpha", "gamma")
    assert graph.has_edge("beta", "alpha")
    assert not graph.has_edge("gamma", "alpha")
    assert not graph.has_edge("gamma", "beta")
    assert graph.number_of_edges() == 3


def test_build_graph_ignores_dangling_links(tmp_vault, mock_config):
    """Links to nonexistent notes are not added as edges, but the source is still a node."""
    _write_note(tmp_vault, "alpha", "Links to [[nonexistent]] and [[beta]].")
    _write_note(tmp_vault, "beta", "No links here.")

    graph = build_wikilink_graph(str(tmp_vault))

    assert "alpha" in graph.nodes
    assert "beta" in graph.nodes
    assert "nonexistent" not in graph.nodes
    assert graph.has_edge("alpha", "beta")
    assert not graph.has_edge("alpha", "nonexistent")


def test_build_graph_empty_vault(tmp_vault, mock_config):
    """Empty notes/ directory produces an empty graph."""
    graph = build_wikilink_graph(str(tmp_vault))

    assert graph.number_of_nodes() == 0
    assert graph.number_of_edges() == 0


# --- compute_pagerank ---


def test_pagerank_star_graph():
    """In a star graph where all spokes link to/from hub, hub has highest PageRank."""
    import networkx as nx

    g = nx.DiGraph()
    for spoke in ("b", "c", "d"):
        g.add_edge("a", spoke)
        g.add_edge(spoke, "a")

    pr = compute_pagerank(g)

    assert pr["a"] > pr["b"]
    assert pr["a"] > pr["c"]
    assert pr["a"] > pr["d"]
    # Spokes should be roughly equal
    assert abs(pr["b"] - pr["c"]) < 0.01


def test_pagerank_empty_graph():
    """Empty graph returns empty dict."""
    import networkx as nx

    g = nx.DiGraph()
    pr = compute_pagerank(g)
    assert pr == {}


# --- serialization round-trip ---


def test_cache_roundtrip(tmp_path):
    """Serialize then deserialize preserves edges and pagerank values."""
    import networkx as nx

    g = nx.DiGraph()
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", "a")

    pr = compute_pagerank(g)
    cache_path = str(tmp_path / "graph-cache.json")

    _serialize_graph(g, pr, cache_path)
    g2, pr2 = _deserialize_graph(cache_path)

    assert set(g2.nodes) == set(g.nodes)
    assert set(g2.edges) == set(g.edges)
    for k in pr:
        assert abs(pr[k] - pr2[k]) < 1e-9


# --- load_or_build_graph ---


def test_load_or_build_uses_cache(tmp_vault, mock_config):
    """Second call to load_or_build_graph uses cache, not a fresh build."""
    _write_note(tmp_vault, "alpha", "See [[beta]].")
    _write_note(tmp_vault, "beta", "No links.")

    cache_path = str(tmp_vault / "test-graph-cache.json")

    with patch("memento.graph._GRAPH_CACHE", new=[None]):
        # First call: builds
        g1, pr1 = load_or_build_graph(
            vault_path=str(tmp_vault),
            cache_path=cache_path,
        )
        assert g1.has_edge("alpha", "beta")

        # Patch build_wikilink_graph so if it gets called we know cache was missed
        with patch("memento.graph.build_wikilink_graph") as mock_build:
            g2, pr2 = load_or_build_graph(
                vault_path=str(tmp_vault),
                cache_path=cache_path,
            )
            mock_build.assert_not_called()

        assert set(g2.edges) == set(g1.edges)


def test_load_or_build_rebuilds_stale_cache(tmp_vault, mock_config):
    """Cache older than 1 hour triggers a rebuild."""
    _write_note(tmp_vault, "alpha", "See [[beta]].")
    _write_note(tmp_vault, "beta", "No links.")

    cache_path = str(tmp_vault / "stale-cache.json")

    import memento.graph as memento_graph

    # Clear in-process cache
    memento_graph._GRAPH_CACHE = [None]

    # Build and write cache
    g, pr = load_or_build_graph(
        vault_path=str(tmp_vault),
        cache_path=cache_path,
    )

    # Backdate cache file to 2 hours ago
    two_hours_ago = time.time() - 7200
    os.utime(cache_path, (two_hours_ago, two_hours_ago))

    # Clear in-process cache so it has to check disk
    memento_graph._GRAPH_CACHE = [None]

    # Add a new note to prove the graph was rebuilt
    _write_note(tmp_vault, "gamma", "See [[alpha]].")

    g2, pr2 = load_or_build_graph(
        vault_path=str(tmp_vault),
        cache_path=cache_path,
    )

    assert "gamma" in g2.nodes
    assert g2.has_edge("gamma", "alpha")


# --- apply_pagerank_boost ---


def test_pagerank_boost_reranks():
    """Two results with same BM25 score; higher pagerank note ranks first."""
    results = [
        {"path": "/vault/notes/note-a.md", "score": 0.5},
        {"path": "/vault/notes/note-b.md", "score": 0.5},
    ]
    pagerank = {"note-a": 0.3, "note-b": 0.01}

    boosted = apply_pagerank_boost(results, pagerank)

    assert boosted is results  # modifies in-place
    assert boosted[0]["path"].endswith("note-a.md")
    assert boosted[1]["path"].endswith("note-b.md")
    assert boosted[0]["score"] > boosted[1]["score"]


def test_pagerank_boost_weight_zero():
    """With pagerank_boost_weight=0, scores should not change."""
    results = [
        {"path": "/vault/notes/alpha.md", "score": 0.8},
        {"path": "/vault/notes/beta.md", "score": 0.6},
    ]
    pagerank = {"alpha": 0.5, "beta": 0.1}

    apply_pagerank_boost(results, pagerank, config={"pagerank_boost_weight": 0})

    assert results[0]["score"] == pytest.approx(0.8)
    assert results[1]["score"] == pytest.approx(0.6)


def test_pagerank_boost_missing_stem():
    """Result with a stem not in pagerank dict has its score unchanged."""
    results = [
        {"path": "/vault/notes/unknown.md", "score": 0.7},
    ]
    pagerank = {"other-note": 0.5}

    apply_pagerank_boost(results, pagerank)

    assert results[0]["score"] == pytest.approx(0.7)


def test_pagerank_boost_preserves_order_when_equal():
    """If pagerank is equal for all results, original score ordering is preserved."""
    results = [
        {"path": "/vault/notes/first.md", "score": 0.9},
        {"path": "/vault/notes/second.md", "score": 0.7},
        {"path": "/vault/notes/third.md", "score": 0.5},
    ]
    pagerank = {"first": 0.1, "second": 0.1, "third": 0.1}

    apply_pagerank_boost(results, pagerank)

    assert results[0]["path"].endswith("first.md")
    assert results[1]["path"].endswith("second.md")
    assert results[2]["path"].endswith("third.md")
