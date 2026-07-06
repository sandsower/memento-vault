"""Tests for wikilink graph building and PageRank computation."""

import os
import time
from unittest.mock import patch

import pytest

from memento.graph import (
    apply_pagerank_boost,
    build_related_view,
    build_wikilink_graph,
    compute_pagerank,
    graph_neighborhood,
    load_or_build_graph,
    resolve_note_reference,
    supersession_chain,
    _deserialize_graph,
    _serialize_graph,
    _vault_notes_max_mtime,
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


def test_load_or_build_rebuilds_when_note_written_after_cache_build(tmp_vault, mock_config):
    """A note written after the cache was built forces a rebuild, even
    within the 1h TTL window (MEM-159: cache validity is now driven by
    note mtimes, with the 1h TTL kept only as an additional ceiling)."""
    _write_note(tmp_vault, "alpha", "See [[beta]].")
    _write_note(tmp_vault, "beta", "No links.")

    cache_path = str(tmp_vault / "fresh-write-cache.json")

    import memento.graph as memento_graph

    memento_graph._GRAPH_CACHE = [None]

    g1, pr1 = load_or_build_graph(vault_path=str(tmp_vault), cache_path=cache_path)
    assert "gamma" not in g1.nodes

    # Clear in-process cache so the next call must consult disk + do the mtime scan.
    memento_graph._GRAPH_CACHE = [None]

    # Write a new note whose mtime is strictly after the cache file's mtime,
    # well within the 1h TTL -- proves staleness is driven by note mtime,
    # not only the TTL ceiling.
    cache_mtime = os.path.getmtime(cache_path)
    new_note_path = _write_note(tmp_vault, "gamma", "See [[alpha]].")
    os.utime(new_note_path, (cache_mtime + 5, cache_mtime + 5))

    with patch("memento.graph.build_wikilink_graph", wraps=memento_graph.build_wikilink_graph) as spy_build:
        g2, pr2 = load_or_build_graph(vault_path=str(tmp_vault), cache_path=cache_path)
        spy_build.assert_called_once()

    assert "gamma" in g2.nodes
    assert g2.has_edge("gamma", "alpha")


def test_load_or_build_reuses_cache_when_no_notes_changed(tmp_vault, mock_config):
    """Sanity check: without any new writes, a cache well within the TTL is
    still reused (mtime check does not force a rebuild every call)."""
    _write_note(tmp_vault, "alpha", "See [[beta]].")
    _write_note(tmp_vault, "beta", "No links.")

    cache_path = str(tmp_vault / "reuse-cache.json")

    import memento.graph as memento_graph

    memento_graph._GRAPH_CACHE = [None]
    load_or_build_graph(vault_path=str(tmp_vault), cache_path=cache_path)
    memento_graph._GRAPH_CACHE = [None]

    with patch("memento.graph.build_wikilink_graph") as mock_build:
        load_or_build_graph(vault_path=str(tmp_vault), cache_path=cache_path)
        mock_build.assert_not_called()


def test_notes_mtime_scan_is_cheap(tmp_vault, mock_config):
    """Benchmark backing the claim in load_or_build_graph's docstring: a
    stat-only scan (no file reads) over ~5k notes stays well under a
    second, so doing it on every cache-validity check is acceptable."""
    notes_dir = tmp_vault / "notes"
    for i in range(5000):
        (notes_dir / f"bench-note-{i}.md").write_text("---\ntitle: x\n---\n\nbody\n")

    start = time.perf_counter()
    result = _vault_notes_max_mtime(str(tmp_vault))
    elapsed = time.perf_counter() - start

    assert result > 0
    assert elapsed < 1.0, f"mtime scan over 5k notes took {elapsed:.3f}s, expected < 1s"


def test_notes_mtime_scan_empty_vault():
    """No notes/ directory returns 0.0 rather than raising."""
    assert _vault_notes_max_mtime("/nonexistent/path/for/sure") == 0.0


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


# --- MEM-159: memento_related topology (resolve_note_reference,
# graph_neighborhood, supersession_chain, build_related_view) ---


@pytest.fixture
def _isolate_graph_cache(tmp_path, monkeypatch):
    """Keep the wikilink graph cache out of the real shared runtime dir.

    build_related_view calls load_or_build_graph(vault) without an explicit
    cache_path, so without this it would read/write the same
    ~/.cache/memento-vault/wikilink-graph.json every other worktree/test in
    this repo uses.
    """
    monkeypatch.setattr("memento.graph._GRAPH_CACHE_PATH", str(tmp_path / "related-view-cache.json"))
    monkeypatch.setattr("memento.graph._GRAPH_CACHE", [None])


class TestResolveNoteReference:
    def test_resolves_by_stem(self, tmp_vault, mock_config, monkeypatch):
        monkeypatch.setattr("memento.graph.get_vault", lambda: tmp_vault)
        _write_note(tmp_vault, "alpha", "See [[beta]].")

        stem, suggestions = resolve_note_reference("alpha")

        assert stem == "alpha"
        assert suggestions == []

    def test_resolves_by_relative_path(self, tmp_vault, mock_config, monkeypatch):
        monkeypatch.setattr("memento.graph.get_vault", lambda: tmp_vault)
        _write_note(tmp_vault, "alpha", "See [[beta]].")

        stem, _ = resolve_note_reference("notes/alpha.md")

        assert stem == "alpha"

    def test_resolves_by_exact_title(self, tmp_vault, mock_config, monkeypatch):
        monkeypatch.setattr("memento.graph.get_vault", lambda: tmp_vault)
        _write_note(tmp_vault, "redis-cache-ttl", "Body.", frontmatter={"title": "Redis cache requires explicit TTL"})

        stem, suggestions = resolve_note_reference("Redis cache requires explicit TTL")

        assert stem == "redis-cache-ttl"
        assert suggestions == []

    def test_unresolved_returns_close_match_suggestions(self, tmp_vault, mock_config, monkeypatch):
        monkeypatch.setattr("memento.graph.get_vault", lambda: tmp_vault)
        _write_note(tmp_vault, "redis-cache-ttl", "Body.")
        _write_note(tmp_vault, "zustand-state-reset", "Body.")

        stem, suggestions = resolve_note_reference("redis-cache-tt")  # typo

        assert stem is None
        assert "redis-cache-ttl" in suggestions

    def test_empty_note_reference_unresolved(self, tmp_vault, mock_config, monkeypatch):
        monkeypatch.setattr("memento.graph.get_vault", lambda: tmp_vault)

        stem, suggestions = resolve_note_reference("")

        assert stem is None
        assert suggestions == []


class TestGraphNeighborhood:
    def _chain_graph(self):
        import networkx as nx

        g = nx.DiGraph()
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        g.add_edge("c", "d")
        g.add_edge("d", "e")
        return g

    def test_depth_clamped_to_max_three(self):
        graph = self._chain_graph()

        entries, truncated = graph_neighborhood(graph, "a", direction="out", depth=10)

        hops = {entry["stem"]: entry["hop"] for entry in entries}
        assert hops == {"b": 1, "c": 2, "d": 3}
        assert "e" not in hops  # hop 4, beyond the clamp
        assert truncated is False

    def test_direction_out_only_follows_successors(self):
        import networkx as nx

        g = nx.DiGraph()
        g.add_edge("hub", "out1")
        g.add_edge("hub", "out2")
        g.add_edge("in1", "hub")
        g.add_edge("in2", "hub")

        entries, _ = graph_neighborhood(g, "hub", direction="out", depth=1)

        assert {e["stem"] for e in entries} == {"out1", "out2"}

    def test_direction_in_only_follows_predecessors(self):
        import networkx as nx

        g = nx.DiGraph()
        g.add_edge("hub", "out1")
        g.add_edge("in1", "hub")
        g.add_edge("in2", "hub")

        entries, _ = graph_neighborhood(g, "hub", direction="in", depth=1)

        assert {e["stem"] for e in entries} == {"in1", "in2"}

    def test_direction_both_follows_both(self):
        import networkx as nx

        g = nx.DiGraph()
        g.add_edge("hub", "out1")
        g.add_edge("in1", "hub")

        entries, _ = graph_neighborhood(g, "hub", direction="both", depth=1)

        assert {e["stem"] for e in entries} == {"out1", "in1"}

    def test_truncation_flag_set_when_cap_exceeded(self):
        import networkx as nx

        g = nx.DiGraph()
        for i in range(5):
            g.add_edge("hub", f"leaf{i}")

        entries, truncated = graph_neighborhood(g, "hub", direction="out", depth=1, cap=3)

        assert len(entries) == 3
        assert truncated is True

    def test_no_neighbors_returns_empty(self):
        import networkx as nx

        g = nx.DiGraph()
        g.add_node("lonely")

        entries, truncated = graph_neighborhood(g, "lonely", direction="both", depth=2)

        assert entries == []
        assert truncated is False

    def test_missing_node_returns_empty(self):
        import networkx as nx

        g = nx.DiGraph()
        g.add_edge("a", "b")

        entries, truncated = graph_neighborhood(g, "nonexistent", direction="both", depth=2)

        assert entries == []
        assert truncated is False


class TestSupersessionChain:
    def test_walks_both_directions_oldest_to_newest(self, tmp_vault, mock_config, monkeypatch):
        monkeypatch.setattr("memento.graph.get_vault", lambda: tmp_vault)
        _write_note(tmp_vault, "note-v1", "First version.")
        _write_note(tmp_vault, "note-v2", "Second version.", frontmatter={"supersedes": '"[[note-v1]]"'})
        _write_note(tmp_vault, "note-v3", "Third version.", frontmatter={"supersedes": '"[[note-v2]]"'})

        chain = supersession_chain("note-v2")

        assert [entry["stem"] for entry in chain] == ["note-v1", "note-v2", "note-v3"]
        assert [entry["hop"] for entry in chain] == [-1, 0, 1]

    def test_no_supersession_returns_only_self(self, tmp_vault, mock_config, monkeypatch):
        monkeypatch.setattr("memento.graph.get_vault", lambda: tmp_vault)
        _write_note(tmp_vault, "standalone", "No supersession here.")

        chain = supersession_chain("standalone")

        assert [entry["stem"] for entry in chain] == ["standalone"]
        assert chain[0]["hop"] == 0


class TestBuildRelatedView:
    def test_full_shape_for_connected_note(self, tmp_vault, mock_config, monkeypatch, _isolate_graph_cache):
        monkeypatch.setattr("memento.graph.get_vault", lambda: tmp_vault)
        _write_note(tmp_vault, "alpha", "See [[beta]].")
        _write_note(tmp_vault, "beta", "No links.")
        _write_note(tmp_vault, "gamma", "See [[alpha]].")

        result = build_related_view("alpha", direction="both", depth=1)

        assert result["note"] == "alpha"
        assert result["path"] == "notes/alpha.md"
        assert [e["stem"] for e in result["outbound"]] == ["beta"]
        assert [e["stem"] for e in result["inbound"]] == ["gamma"]
        neighborhood_stems = {e["stem"] for e in result["neighborhood"]["nodes"]}
        assert neighborhood_stems == {"beta", "gamma"}
        assert result["neighborhood"]["truncated"] is False
        assert [e["stem"] for e in result["supersession_chain"]] == ["alpha"]
        assert "error" not in result

    def test_depth_is_clamped_in_response(self, tmp_vault, mock_config, monkeypatch, _isolate_graph_cache):
        monkeypatch.setattr("memento.graph.get_vault", lambda: tmp_vault)
        _write_note(tmp_vault, "alpha", "See [[beta]].")
        _write_note(tmp_vault, "beta", "No links.")

        result = build_related_view("alpha", depth=99)

        assert result["depth"] == 3

    def test_unresolved_note_returns_structured_error(self, tmp_vault, mock_config, monkeypatch, _isolate_graph_cache):
        monkeypatch.setattr("memento.graph.get_vault", lambda: tmp_vault)
        _write_note(tmp_vault, "redis-cache-ttl", "Body.")

        result = build_related_view("redis-cache-tt")  # typo

        assert result["reason"] == "note_not_found"
        assert "error" in result
        assert "redis-cache-ttl" in result["suggestions"]

    def test_networkx_unavailable_returns_structured_error(self, tmp_vault, mock_config, monkeypatch):
        monkeypatch.setattr("memento.graph.get_vault", lambda: tmp_vault)
        monkeypatch.setattr("memento.graph._HAS_NETWORKX", False)

        result = build_related_view("alpha")

        assert result["reason"] == "networkx_unavailable"
        assert "error" in result
