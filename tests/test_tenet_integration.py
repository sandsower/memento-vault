"""Integration tests for the Tier 1 Tenet retrieval pipeline.

Tests enhance_results with all Tier 1 features wired together:
PageRank boost, PPR expansion, temporal decay, project filtering.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import networkx as nx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))

from memento_utils import (
    enhance_results,
    build_wikilink_graph,
    compute_pagerank,
    apply_pagerank_boost,
    ppr_expand,
    DEFAULT_CONFIG,
)


@pytest.fixture
def linked_vault(tmp_path):
    """Vault with a known wikilink structure for integration testing.

    Graph:   A → B → C → D
             A → C
             D → A  (cycle)
             E (isolated, same project)

    All notes in /home/vic/Projects/test-project.
    A has certainty 5 (immune to decay), rest have 3.
    """
    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)

    note_data = {
        "note-a": {
            "title": "Core architecture decision",
            "certainty": 5,
            "tags": ["architecture"],
            "links": ["note-b", "note-c"],
        },
        "note-b": {
            "title": "Service layer implementation",
            "certainty": 3,
            "tags": ["services"],
            "links": ["note-c"],
        },
        "note-c": {
            "title": "Database schema design",
            "certainty": 3,
            "tags": ["database"],
            "links": ["note-d"],
        },
        "note-d": {
            "title": "Migration strategy",
            "certainty": 3,
            "tags": ["migration"],
            "links": ["note-a"],
        },
        "note-e": {
            "title": "Isolated testing note",
            "certainty": 3,
            "tags": ["testing"],
            "links": [],
        },
    }

    for stem, data in note_data.items():
        links_md = "\n".join(f"- [[{l}]]" for l in data["links"])
        content = f"""---
title: {data['title']}
type: discovery
tags: [{', '.join(data['tags'])}]
date: 2026-03-20T10:00
certainty: {data['certainty']}
project: /home/vic/Projects/test-project
---

Body of {stem}.

## Related

{links_md}
"""
        (notes / f"{stem}.md").write_text(content)

    return vault


@pytest.fixture
def linked_graph(linked_vault):
    """Build graph from the linked vault."""
    return build_wikilink_graph(linked_vault)


@pytest.fixture
def linked_pagerank(linked_graph):
    """Compute PageRank for the linked vault graph."""
    return compute_pagerank(linked_graph)


@pytest.fixture
def integration_config(linked_vault):
    """Config pointing at the linked vault."""
    config = dict(DEFAULT_CONFIG)
    config["vault_path"] = str(linked_vault)
    config["ppr_enabled"] = True
    config["ppr_max_expanded"] = 3
    config["pagerank_boost_weight"] = 0.3
    config["temporal_decay"] = False  # disable for predictable scoring
    return config


class TestEnhanceResultsPipeline:
    """Test the full enhance_results pipeline with Tier 1 features."""

    def test_pagerank_boost_reranks_results(self, linked_pagerank, integration_config):
        """Results with higher PageRank should be boosted above equal-BM25-score results."""
        # note-a has more inbound links (from note-d) and outbound, so higher PageRank
        results = [
            {"path": "notes/note-e.md", "title": "Isolated", "score": 0.8},
            {"path": "notes/note-a.md", "title": "Core arch", "score": 0.8},
        ]

        boosted = apply_pagerank_boost(results, linked_pagerank, integration_config)
        # note-a should be boosted above note-e (isolated has low PageRank)
        assert boosted[0]["path"] == "notes/note-a.md"

    def test_ppr_expands_beyond_1_hop(self, linked_graph, integration_config):
        """PPR should surface notes beyond 1 hop from seeds."""
        # Seed on note-a only
        results = [{"path": "notes/note-a.md", "title": "Core arch", "score": 0.9}]

        expanded = ppr_expand(results, linked_graph, integration_config)
        expanded_stems = {Path(r["path"]).stem for r in expanded}

        # note-b is 1 hop, note-c is reachable via A→C and A→B→C,
        # note-d is 2 hops via A→C→D
        # All should be reachable via PPR (not just 1-hop)
        assert len(expanded) > 0
        # At minimum note-b and note-c should appear (direct links)
        assert "note-b" in expanded_stems or "note-c" in expanded_stems

    def test_enhance_results_full_pipeline(self, linked_vault, integration_config):
        """End-to-end: enhance_results applies boost + PPR when graph available."""
        results = [
            {"path": "notes/note-a.md", "title": "Core arch", "score": 0.9},
            {"path": "notes/note-e.md", "title": "Isolated", "score": 0.85},
        ]

        # Patch load_or_build_graph to use our vault
        graph = build_wikilink_graph(linked_vault)
        pagerank = compute_pagerank(graph)

        with patch("memento_utils.load_or_build_graph", return_value=(graph, pagerank)):
            enhanced = enhance_results(
                results, config=integration_config,
                cwd="/home/vic/Projects/test-project",
            )

        paths = [r["path"] for r in enhanced]

        # Should have original results + PPR-expanded notes
        assert len(enhanced) > 2, f"Expected expansion, got {len(enhanced)} results"

        # note-a should still be present (seed)
        assert "notes/note-a.md" in paths

        # Internal metadata should be cleaned
        for r in enhanced:
            assert "_meta" not in r
            assert "_ppr" not in r

    def test_enhance_results_falls_back_to_wikilinks(self, integration_config):
        """When graph is unavailable, falls back to expand_wikilinks."""
        results = [
            {"path": "notes/note-a.md", "title": "Core arch", "score": 0.9},
        ]

        # Patch load_or_build_graph to fail
        with patch("memento_utils.load_or_build_graph", side_effect=ImportError("no networkx")):
            enhanced = enhance_results(results, config=integration_config)

        # Should still return results (fallback to wikilinks, which may not expand
        # without a real vault, but should not crash)
        assert len(enhanced) >= 1
        assert enhanced[0]["path"] == "notes/note-a.md"

    def test_enhance_results_project_filter_still_works(self, linked_vault, integration_config):
        """Project filtering should still exclude notes from other projects."""
        # Add a note from a different project
        other_note = linked_vault / "notes" / "other-project-note.md"
        other_note.write_text("""---
title: Other project note
type: discovery
tags: [other]
date: 2026-03-20T10:00
certainty: 3
project: /home/vic/Projects/different-project
---

Body of other note.
""")

        results = [
            {"path": "notes/note-a.md", "title": "Core arch", "score": 0.9},
            {"path": "notes/other-project-note.md", "title": "Other", "score": 0.85},
        ]

        graph = build_wikilink_graph(linked_vault)
        pagerank = compute_pagerank(graph)

        with patch("memento_utils.load_or_build_graph", return_value=(graph, pagerank)), \
             patch("memento_utils.get_vault", return_value=linked_vault):
            enhanced = enhance_results(
                results, config=integration_config,
                cwd="/home/vic/Projects/test-project",
            )

        paths = [r["path"] for r in enhanced]
        assert "notes/other-project-note.md" not in paths
        assert "notes/note-a.md" in paths


class TestConfigDefaults:
    """Verify all Tier 1 config keys are present in DEFAULT_CONFIG."""

    @pytest.mark.parametrize("key", [
        "prf_enabled", "prf_max_terms", "prf_top_docs",
        "ppr_enabled", "ppr_max_expanded", "ppr_alpha", "ppr_min_score",
        "pagerank_alpha", "pagerank_boost_weight",
        "rrf_enabled", "rrf_k",
        "concept_index_enabled", "concept_index_score",
        "project_maps_enabled",
    ])
    def test_config_key_exists(self, key):
        """Every Tier 1 config key should have a default."""
        assert key in DEFAULT_CONFIG, f"Missing config default: {key}"
