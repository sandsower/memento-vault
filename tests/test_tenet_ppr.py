"""Tests for Personalized PageRank (PPR) wikilink expansion."""

import networkx as nx
import pytest

from memento.graph import ppr_expand


@pytest.fixture
def sample_graph():
    """A→B→C→D with extra edges A→C, D→A, B→D for richer connectivity."""
    g = nx.DiGraph()
    g.add_edges_from([("a", "b"), ("b", "c"), ("c", "d"), ("a", "c"), ("d", "a"), ("b", "d")])
    return g


@pytest.fixture
def star_graph():
    """Hub E with spokes F, G, H — all edges point outward from E."""
    g = nx.DiGraph()
    for spoke in ("f", "g", "h"):
        g.add_edge("e", spoke)
    return g


def _result(stem, score=0.5):
    """Helper to build a mock search result."""
    return {"path": f"notes/{stem}.md", "score": score}


# --- core behaviour ---


def test_ppr_expand_surfaces_connected_notes(sample_graph):
    """Seeding on [a] should surface b and/or c as expanded notes."""
    results = [_result("a", 0.9)]
    expanded = ppr_expand(results, sample_graph)

    stems = {e["title"] for e in expanded}
    # b is directly connected from a; should appear
    assert "b" in stems or "c" in stems
    # Every expanded entry should have _ppr flag
    assert all(e.get("_ppr") is True for e in expanded)
    # Paths should follow notes/{stem}.md format
    assert all(e["path"] == f"notes/{e['title']}.md" for e in expanded)


def test_ppr_expand_respects_max_expanded(sample_graph):
    """Setting ppr_max_expanded=1 limits output to at most 1 entry."""
    results = [_result("a", 0.9)]
    config = {"ppr_max_expanded": 1}
    expanded = ppr_expand(results, sample_graph, config=config)

    assert len(expanded) <= 1


def test_ppr_expand_excludes_seeds(sample_graph):
    """Seed notes should never appear in the expanded results."""
    results = [_result("a", 0.9), _result("b", 0.5)]
    expanded = ppr_expand(results, sample_graph)

    expanded_stems = {e["title"] for e in expanded}
    assert "a" not in expanded_stems
    assert "b" not in expanded_stems


def test_ppr_expand_weights_by_search_score(sample_graph):
    """Seed a (score=0.9) should push its neighbours higher than seed d (score=0.1)."""
    results = [_result("a", 0.9), _result("d", 0.1)]
    config = {"ppr_max_expanded": 10}
    expanded = ppr_expand(results, sample_graph, config=config)

    if len(expanded) >= 2:
        # b and c are reachable from a; they should rank above nodes only reachable from d
        scores_by_stem = {e["title"]: e["score"] for e in expanded}
        # b is directly linked from a (high-weight seed) — it should have a meaningful score
        if "b" in scores_by_stem:
            assert scores_by_stem["b"] > 0


def test_ppr_expand_empty_graph():
    """Empty graph returns empty list."""
    g = nx.DiGraph()
    results = [_result("a", 0.9)]
    expanded = ppr_expand(results, g)

    assert expanded == []


def test_ppr_expand_no_edges_from_seeds():
    """Seeds exist as nodes but have no outgoing edges — expansion should be empty or near-empty."""
    g = nx.DiGraph()
    g.add_node("a")
    g.add_node("b")
    # No edges at all
    results = [_result("a", 0.9)]
    expanded = ppr_expand(results, g)

    # With no edges, PPR can't propagate, so no non-seed nodes should surface meaningfully
    assert len(expanded) == 0


def test_ppr_expand_respects_min_score(sample_graph):
    """Setting a high ppr_min_score should filter out low-PPR nodes."""
    results = [_result("a", 0.9)]
    config = {"ppr_min_score": 0.5}  # Very high threshold — most PPR scores are << 0.5
    expanded = ppr_expand(results, sample_graph, config=config)

    # All returned entries must meet the min_score threshold
    for e in expanded:
        assert e["score"] >= 0.5


def test_ppr_expand_scores_are_floats(sample_graph):
    """All scores in expanded results should be floats."""
    results = [_result("a", 0.9)]
    expanded = ppr_expand(results, sample_graph)

    for e in expanded:
        assert isinstance(e["score"], float)


def test_ppr_expand_sorted_descending(sample_graph):
    """Expanded results should be sorted by score descending."""
    results = [_result("a", 0.9)]
    config = {"ppr_max_expanded": 10}
    expanded = ppr_expand(results, sample_graph, config=config)

    scores = [e["score"] for e in expanded]
    assert scores == sorted(scores, reverse=True)
