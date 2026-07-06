"""Tests for memento/search.py retrieval enhancements.

MEM-150: apply_temporal_decay() now derives decay immunity from the derived
durability tier (memento.store.durability_tier), not certainty. This file
covers the decay-immunity matrix (pinned/hot immune; warm/cold decay
normally, including a certainty-5 cold note) and the hot-window config knob.
"""

from datetime import datetime, timedelta, timezone

import pytest

from memento.search import apply_temporal_decay


def _iso_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_note(vault, stem, *, certainty, date="2020-01-01T00:00", extra_frontmatter=""):
    note = vault / "notes" / f"{stem}.md"
    note.write_text(
        "---\n"
        "title: Example\n"
        "type: discovery\n"
        "tags: []\n"
        f"certainty: {certainty}\n"
        f"date: {date}\n"
        f"{extra_frontmatter}"
        "---\n\nBody.\n"
    )
    return note


@pytest.fixture(autouse=True)
def _patch_vault_config(monkeypatch, tmp_vault):
    """Point get_vault()/read_note_metadata() at the test's tmp vault.

    apply_temporal_decay() calls get_vault() (and, via graph.read_note_metadata,
    the same function) unconditionally -- both resolve through
    memento.config.get_config() at call time, so patching that one binding is
    enough regardless of the `config` dict passed explicitly to
    apply_temporal_decay itself.
    """
    cfg = {"vault_path": str(tmp_vault)}
    monkeypatch.setattr("memento.config.get_config", lambda: cfg, raising=False)
    return cfg


def _decay_config(**overrides):
    cfg = {
        "temporal_decay": True,
        "temporal_decay_half_life": 90,
        "temporal_decay_undated_factor": 0.5,
        "durability_hot_window_days": 30,
    }
    cfg.update(overrides)
    return cfg


class TestApplyTemporalDecayDurabilityTiers:
    """MEM-150: decay immunity matrix (pinned/hot immune; warm/cold decay)."""

    def test_pinned_is_decay_immune_even_at_low_certainty(self, tmp_vault):
        _write_note(tmp_vault, "pinned-note", certainty=1, extra_frontmatter="pinned: true\n")
        results = [{"path": "notes/pinned-note.md", "score": 1.0}]

        apply_temporal_decay(results, config=_decay_config())

        assert results[0]["score"] == 1.0
        assert results[0]["_durability_tier"] == "pinned"

    def test_hot_is_decay_immune_even_at_low_certainty(self, tmp_vault):
        _write_note(
            tmp_vault,
            "hot-note",
            certainty=1,
            extra_frontmatter=f"resurfaced_count: 4\nlast_resurfaced: {_iso_days_ago(1)}\n",
        )
        results = [{"path": "notes/hot-note.md", "score": 1.0}]

        apply_temporal_decay(results, config=_decay_config())

        assert results[0]["score"] == 1.0
        assert results[0]["_durability_tier"] == "hot"

    def test_warm_decays_despite_high_certainty(self, tmp_vault):
        _write_note(
            tmp_vault,
            "warm-note",
            certainty=5,
            extra_frontmatter=f"resurfaced_count: 4\nlast_resurfaced: {_iso_days_ago(200)}\n",
        )
        results = [{"path": "notes/warm-note.md", "score": 1.0}]

        apply_temporal_decay(results, config=_decay_config())

        assert results[0]["score"] < 1.0
        assert results[0]["_durability_tier"] == "warm"

    def test_cold_certainty_5_decays(self, tmp_vault):
        """Acceptance (MEM-150): a certainty-5 note never resurfaced sinks like any other."""
        _write_note(tmp_vault, "cold-note", certainty=5)
        results = [{"path": "notes/cold-note.md", "score": 1.0}]

        apply_temporal_decay(results, config=_decay_config())

        assert results[0]["score"] < 1.0
        assert results[0]["_durability_tier"] == "cold"

    def test_cold_and_warm_decay_by_the_same_formula_certainty_aside(self, tmp_vault):
        """Certainty no longer changes whether a note decays, only (at certainty 3) how fast."""
        _write_note(tmp_vault, "cold-c1", certainty=1)
        _write_note(tmp_vault, "cold-c5", certainty=5)
        results = [
            {"path": "notes/cold-c1.md", "score": 1.0},
            {"path": "notes/cold-c5.md", "score": 1.0},
        ]

        apply_temporal_decay(results, config=_decay_config())

        by_path = {r["path"]: r for r in results}
        assert by_path["notes/cold-c1.md"]["score"] < 1.0
        assert by_path["notes/cold-c5.md"]["score"] < 1.0
        assert by_path["notes/cold-c1.md"]["score"] == pytest.approx(by_path["notes/cold-c5.md"]["score"])


class TestApplyTemporalDecayHotWindowConfigKnob:
    def test_hot_window_config_knob_changes_tier_and_decay(self, tmp_vault):
        """Same last_resurfaced timestamp classifies differently under a narrower window."""
        ts = _iso_days_ago(20)
        _write_note(
            tmp_vault,
            "window-note",
            certainty=3,
            extra_frontmatter=f"resurfaced_count: 2\nlast_resurfaced: {ts}\n",
        )

        wide_results = [{"path": "notes/window-note.md", "score": 1.0}]
        apply_temporal_decay(wide_results, config=_decay_config(durability_hot_window_days=30))
        assert wide_results[0]["_durability_tier"] == "hot"
        assert wide_results[0]["score"] == 1.0

        narrow_results = [{"path": "notes/window-note.md", "score": 1.0}]
        apply_temporal_decay(narrow_results, config=_decay_config(durability_hot_window_days=10))
        assert narrow_results[0]["_durability_tier"] == "warm"
        assert narrow_results[0]["score"] < 1.0
