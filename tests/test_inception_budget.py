"""Tests for Inception's per-run processing budget (MEM-154).

Covers compute_inception_budget (ties the budget to capture volume, with
configurable floor/cap) and select_clusters_within_budget (the note-count
based replacement for the old fixed scored[:max_clusters] slice).
"""

from memento_inception import compute_inception_budget, select_clusters_within_budget


class TestComputeInceptionBudget:
    def test_uses_floor_when_ingest_is_low(self):
        """Quiet period: ingest count is below the floor, floor wins."""
        config = {"inception_budget_floor": 20, "inception_budget_cap": 200}
        assert compute_inception_budget(config, notes_ingested=3) == 20

    def test_uses_ingest_count_when_above_floor(self):
        """Busy period: ingest count exceeds the floor, ingest count wins."""
        config = {"inception_budget_floor": 20, "inception_budget_cap": 200}
        assert compute_inception_budget(config, notes_ingested=80) == 80

    def test_caps_at_hard_cap(self):
        """A backlog catch-up burst is still bounded by the hard cap."""
        config = {"inception_budget_floor": 20, "inception_budget_cap": 200}
        assert compute_inception_budget(config, notes_ingested=5000) == 200

    def test_defaults_when_config_keys_absent(self):
        """Missing config keys fall back to sane defaults, not a crash."""
        budget = compute_inception_budget({}, notes_ingested=0)
        assert budget > 0

    def test_zero_ingest_still_returns_floor(self):
        config = {"inception_budget_floor": 15, "inception_budget_cap": 100}
        assert compute_inception_budget(config, notes_ingested=0) == 15

    def test_floor_larger_than_cap_is_still_bounded_by_cap(self):
        """Pathological config (floor > cap): cap always wins, never crashes."""
        config = {"inception_budget_floor": 500, "inception_budget_cap": 50}
        assert compute_inception_budget(config, notes_ingested=10) == 50


class TestSelectClustersWithinBudget:
    def test_selects_until_budget_met(self):
        scored = [
            ("c1", ["a", "b", "c"], 3.0),
            ("c2", ["d", "e"], 2.0),
            ("c3", ["f", "g", "h"], 1.0),
        ]
        selected = select_clusters_within_budget(scored, note_budget=4)
        # c1 alone is 3 notes (< 4), so c2 is added (3+2=5 >= 4), then stop.
        assert [item[0] for item in selected] == ["c1", "c2"]

    def test_always_includes_at_least_one_cluster(self):
        """A single cluster larger than the budget is still processed."""
        scored = [("only", ["a", "b", "c", "d", "e"], 5.0)]
        selected = select_clusters_within_budget(scored, note_budget=1)
        assert [item[0] for item in selected] == ["only"]

    def test_empty_input_returns_empty(self):
        assert select_clusters_within_budget([], note_budget=100) == []

    def test_large_budget_selects_everything(self):
        scored = [("c1", ["a"], 3.0), ("c2", ["b"], 2.0), ("c3", ["c"], 1.0)]
        selected = select_clusters_within_budget(scored, note_budget=1000)
        assert len(selected) == 3

    def test_preserves_score_order(self):
        """Selection respects the caller's pre-sorted (score descending) order."""
        scored = [("high", ["a"], 9.0), ("mid", ["b"], 5.0), ("low", ["c"], 1.0)]
        selected = select_clusters_within_budget(scored, note_budget=2)
        assert [item[0] for item in selected] == ["high", "mid"]
