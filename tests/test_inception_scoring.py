"""Tests for Inception cluster scoring function."""

from pathlib import Path

import pytest

from memento_inception import NoteRecord, score_cluster


def _make_record(stem, *, tags=None, date="", certainty=None, project=None):
    """Build a minimal NoteRecord for scoring tests."""
    return NoteRecord(
        stem=stem,
        path=Path(f"/fake/notes/{stem}.md"),
        title=f"Title for {stem}",
        note_type="discovery",
        tags=tags or [],
        date=date,
        certainty=certainty,
        project=project,
    )


def _notes_dict(*records):
    """Build a stem -> NoteRecord dict from a list of records."""
    return {r.stem: r for r in records}


# ── test_empty_cluster ──────────────────────────────────────────────


def test_empty_cluster():
    """Empty stems list returns 0.0."""
    assert score_cluster([], {}) == 0.0


# ── test_single_note ────────────────────────────────────────────────


def test_single_note():
    """Single note: size=log2(1)=0, no temporal spread, no project bonus."""
    rec = _make_record("a", tags=["foo"], date="2026-03-10T14:00", certainty=4)
    nd = _notes_dict(rec)
    score = score_cluster(["a"], nd)

    # size_score = log2(1) = 0.0
    # tag_diversity = 1/1 = 1.0  -> *0.8 = 0.8
    # temporal_score = 0.0 (only 1 date) -> *0.6 = 0.0
    # project_bonus = 0.0 -> *0.5 = 0.0
    # certainty = 4/5 = 0.8 -> *0.3 = 0.24
    expected = 0.0 + 0.8 + 0.0 + 0.0 + 0.24
    assert score == pytest.approx(expected, abs=1e-6)


# ── test_larger_cluster_higher_score ────────────────────────────────


def test_larger_cluster_higher_score():
    """5 notes scores higher than 3 notes with same per-note attributes."""
    attrs = dict(tags=["tag1"], date="2026-03-10T14:00", certainty=3, project="projA")

    small = [_make_record(f"s{i}", **attrs) for i in range(3)]
    big = [_make_record(f"b{i}", **attrs) for i in range(5)]

    small_score = score_cluster([r.stem for r in small], _notes_dict(*small))
    big_score = score_cluster([r.stem for r in big], _notes_dict(*big))

    assert big_score > small_score


# ── test_cross_project_bonus ────────────────────────────────────────


def test_cross_project_bonus():
    """Cluster spanning 2 projects scores higher than a single-project cluster."""
    single = [
        _make_record("a", tags=["t"], date="2026-03-01", certainty=3, project="proj1"),
        _make_record("b", tags=["t"], date="2026-03-02", certainty=3, project="proj1"),
    ]
    multi = [
        _make_record("c", tags=["t"], date="2026-03-01", certainty=3, project="proj1"),
        _make_record("d", tags=["t"], date="2026-03-02", certainty=3, project="proj2"),
    ]

    single_score = score_cluster([r.stem for r in single], _notes_dict(*single))
    multi_score = score_cluster([r.stem for r in multi], _notes_dict(*multi))

    assert multi_score > single_score
    # The exact difference should be the project bonus weight (0.5 * 0.5 = 0.25)
    assert multi_score - single_score == pytest.approx(0.25, abs=1e-6)


# ── test_temporal_spread ────────────────────────────────────────────


def test_temporal_spread():
    """Cluster spanning 15 days scores higher than same-day cluster."""
    same_day = [
        _make_record("a", tags=["t"], date="2026-03-10T14:00", certainty=3),
        _make_record("b", tags=["t"], date="2026-03-10T16:00", certainty=3),
    ]
    spread = [
        _make_record("c", tags=["t"], date="2026-03-01T10:00", certainty=3),
        _make_record("d", tags=["t"], date="2026-03-16T10:00", certainty=3),
    ]

    same_score = score_cluster([r.stem for r in same_day], _notes_dict(*same_day))
    spread_score = score_cluster([r.stem for r in spread], _notes_dict(*spread))

    assert spread_score > same_score


# ── test_tag_diversity ──────────────────────────────────────────────


def test_tag_diversity():
    """Cluster with 5 unique tags scores higher than 1 repeated tag."""
    repeated = [
        _make_record("a", tags=["dup", "dup"], date="2026-03-10", certainty=3),
        _make_record("b", tags=["dup", "dup"], date="2026-03-11", certainty=3),
    ]
    diverse = [
        _make_record("c", tags=["t1", "t2"], date="2026-03-10", certainty=3),
        _make_record("d", tags=["t3", "t4", "t5"], date="2026-03-11", certainty=3),
    ]

    rep_score = score_cluster([r.stem for r in repeated], _notes_dict(*repeated))
    div_score = score_cluster([r.stem for r in diverse], _notes_dict(*diverse))

    assert div_score > rep_score


# ── test_missing_dates ──────────────────────────────────────────────


def test_missing_dates():
    """Notes with empty dates don't crash; temporal_score = 0."""
    recs = [
        _make_record("a", tags=["t"], date="", certainty=3),
        _make_record("b", tags=["t"], date="", certainty=3),
    ]
    nd = _notes_dict(*recs)
    score = score_cluster(["a", "b"], nd)

    # Should not raise; temporal component should be 0
    # size_score = log2(2) = 1.0
    # tag_diversity = 1/2 = 0.5 -> *0.8 = 0.4
    # temporal_score = 0.0 -> *0.6 = 0.0
    # project_bonus = 0.0 -> *0.5 = 0.0
    # certainty = 3/5 = 0.6 -> *0.3 = 0.18
    expected = 1.0 + 0.4 + 0.0 + 0.0 + 0.18
    assert score == pytest.approx(expected, abs=1e-6)


# ── test_missing_certainty ─────────────────────────────────────────


def test_missing_certainty():
    """Notes with None certainty use 0.5 default."""
    recs = [
        _make_record("a", tags=["t"], date="2026-03-10", certainty=None),
        _make_record("b", tags=["t"], date="2026-03-11", certainty=None),
    ]
    nd = _notes_dict(*recs)
    score = score_cluster(["a", "b"], nd)

    # certainty_score should be 0.5 (default)
    # size = log2(2) = 1.0
    # tag_diversity = 1/2 = 0.5 -> *0.8 = 0.4
    # temporal = 1/30 capped -> *0.6
    # project_bonus = 0.0
    # certainty = 0.5 -> *0.3 = 0.15
    temporal = min(1 / 30.0, 1.0) * 0.6
    expected = 1.0 + 0.4 + temporal + 0.0 + 0.15
    assert score == pytest.approx(expected, abs=1e-6)
