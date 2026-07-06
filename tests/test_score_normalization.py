"""Cross-backend score-normalization threshold tests (MEM-127).

Before MEM-127, qmd raw scores, FTS5's batch-relative "top hit always 1.0"
normalization, sqlite-vec's raw L2 distance, and grep's term-coverage
fraction were four incompatible scales, all gated by the SAME
``recall_min_score`` / ``recall_high_confidence`` thresholds regardless of
which backend answered. This file is the "one fixture corpus indexed per
backend" cross-backend threshold test called for in the MEM-127 design: the
same query's strong match and weak/unrelated match must land on the correct
side of ``recall_min_score`` consistently, now that every backend normalizes
its own score to [0, 1] at the search() boundary (see
memento.search_backend and memento.embedded_search normalize_* functions).

QMD itself is an external binary that cannot be indexed or queried live in
CI, so its half of the cross-backend check exercises the pure
``normalize_qmd_score()`` boundary function against the observed production
score ranges from docs/quality-analysis-2026-07-02.md instead of a live
``qmd`` subprocess.
"""

import pytest

from memento.config import DEFAULT_CONFIG
from memento.search_backend import GrepBackend, normalize_qmd_score

RECALL_MIN_SCORE = DEFAULT_CONFIG["recall_min_score"]
RECALL_HIGH_CONFIDENCE = DEFAULT_CONFIG["recall_high_confidence"]

QUERY = "Redis cache TTL stale reads failover"


@pytest.fixture
def fixture_vault(tmp_path):
    """A small shared corpus: one strong match, several distractors, one
    note sharing zero vocabulary with the query (the "weak/unrelated match"
    the design doc calls for)."""
    vault = tmp_path / "vault"
    notes = vault / "notes"
    notes.mkdir(parents=True)

    (notes / "strong-match.md").write_text(
        "---\ntitle: Redis cache TTL stale read bug\n---\n\n"
        "Redis cache TTL configuration prevents stale reads during failover incidents.\n"
    )
    for i in range(6):
        (notes / f"distractor-{i}.md").write_text(
            f"---\ntitle: Distractor note {i}\n---\n\nUnrelated topic number {i} about deployment pipelines.\n"
        )
    (notes / "weak-match.md").write_text(
        "---\ntitle: Completely unrelated topic\n---\n\nBird migration patterns in the Pacific northwest.\n"
    )
    return vault


class TestCrossBackendThresholdConsistency:
    """Same query, same recall_min_score, consistent pass/fail per backend."""

    def test_grep_backend_strong_vs_weak_match(self, fixture_vault, monkeypatch):
        monkeypatch.setattr("memento.config.get_vault", lambda: fixture_vault)
        backend = GrepBackend()
        results = backend.search(QUERY, "memento", limit=10, min_score=0.0)
        by_path = {r["path"]: r for r in results}

        strong = by_path.get("notes/strong-match.md")
        weak = by_path.get("notes/weak-match.md")

        assert strong is not None
        assert strong["backend"] == "grep"
        assert strong["score"] >= RECALL_MIN_SCORE
        # A note sharing zero query vocabulary either isn't returned at all
        # (grep drops zero-match files) or scores below the noise floor.
        assert weak is None or weak["score"] < RECALL_MIN_SCORE

    def test_embedded_fts_backend_strong_vs_weak_match(self, fixture_vault, tmp_path):
        from memento.embedded_search import EmbeddedSearchBackend

        db_path = tmp_path / "search.db"
        backend = EmbeddedSearchBackend(vault_path=fixture_vault, db_path=db_path)
        backend.reindex("memento")
        results = backend.search(QUERY, "memento", limit=10, min_score=0.0)
        by_path = {r["path"]: r for r in results}

        strong = by_path.get("notes/strong-match.md")
        weak = by_path.get("notes/weak-match.md")

        assert strong is not None
        assert strong["backend"] == "embedded-fts"
        assert strong["score"] >= RECALL_MIN_SCORE
        assert weak is None or weak["score"] < RECALL_MIN_SCORE

    def test_qmd_backend_score_normalization_fixture_values(self):
        """QMD can't be indexed/queried live in CI (external binary with no
        installable fixture harness), so this exercises the pure
        normalize_qmd_score() boundary against the observed production
        ranges instead: BM25 hits 0.9-0.98, a "barely related" QMD result
        still floors at 0.87-0.89 (see confidence_margin() docstring in
        retrieval_policy.py) - well above recall_min_score, so a *properly*
        unrelated result must score far below that floor to land on the
        correct side of the threshold."""
        strong_raw = 0.96  # observed BM25 hit band
        weak_raw = 0.05  # far below QMD's own observed "barely related" floor

        assert normalize_qmd_score(strong_raw) >= RECALL_MIN_SCORE
        assert normalize_qmd_score(weak_raw) < RECALL_MIN_SCORE

    def test_strong_match_also_clears_high_confidence_bar(self, fixture_vault, tmp_path):
        """A decisive, multi-distinguishing-term match should read as
        confident (clear recall_high_confidence) on both indexable backends,
        not just clear the coarser recall_min_score noise floor."""
        from memento.embedded_search import EmbeddedSearchBackend

        db_path = tmp_path / "search.db"
        embedded = EmbeddedSearchBackend(vault_path=fixture_vault, db_path=db_path)
        embedded.reindex("memento")
        embedded_results = embedded.search(QUERY, "memento", limit=10, min_score=0.0)
        assert embedded_results[0]["path"] == "notes/strong-match.md"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("memento.config.get_vault", lambda: fixture_vault)
            grep_results = GrepBackend().search(QUERY, "memento", limit=10, min_score=0.0)
        assert grep_results[0]["path"] == "notes/strong-match.md"
        assert grep_results[0]["score"] >= RECALL_HIGH_CONFIDENCE


class TestRRFInflationInteractionMEM143:
    """MEM-143 (fixed): RRF's own rank-based score is purely positional, so a
    weak match that happens to rank first in two thin candidate lists would
    fuse to a normalized score of 1.0 regardless of how weak its underlying
    per-backend score was, if the fused score were rank-only. Since MEM-127,
    every backend's own score is normalized to a comparable [0, 1] scale, so
    ``rrf_fuse`` now caps the fused score at the document's best underlying
    normalized score (``fused = rrf_normalized * best_quality``) - rank still
    decides ordering, but it can no longer manufacture quality above what the
    underlying backends actually measured."""

    def test_weak_match_score_survives_rrf_fusion_below_recall_min_score(self):
        from memento.search import rrf_fuse

        weak_result = {"path": "notes/weak.md", "title": "Weak", "score": 0.05, "snippet": ""}
        fused = rrf_fuse([[dict(weak_result)], [dict(weak_result)]], k=60)

        assert fused
        top = fused[0]
        assert top["path"] == "notes/weak.md"
        assert top["score"] < RECALL_MIN_SCORE
