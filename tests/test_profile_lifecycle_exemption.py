"""Phase 3: profile (curated) notes are exempt from decay, sweep, and resurfacing."""

from __future__ import annotations

from memento import config
from memento.config import is_curated_path


def _cfg(**profile_overrides):
    base = dict(config.DEFAULT_CONFIG)
    base["profile"] = {**config.DEFAULT_CONFIG["profile"], **profile_overrides}
    return base


def test_is_curated_path_true_for_profile():
    assert is_curated_path("profile/voice.md", config.DEFAULT_CONFIG) is True


def test_is_curated_path_false_for_regular_dirs():
    assert is_curated_path("notes/foo.md", config.DEFAULT_CONFIG) is False
    assert is_curated_path("fleeting/x.md", config.DEFAULT_CONFIG) is False
    assert is_curated_path("projects/bar.md", config.DEFAULT_CONFIG) is False
    assert is_curated_path("archive/old.md", config.DEFAULT_CONFIG) is False


def test_is_curated_path_custom_dir():
    cfg = _cfg(dir="persona")
    assert is_curated_path("persona/voice.md", cfg) is True
    assert is_curated_path("profile/voice.md", cfg) is False


def test_is_curated_path_edge_inputs():
    assert is_curated_path("", config.DEFAULT_CONFIG) is False
    assert is_curated_path("voice.md", config.DEFAULT_CONFIG) is False  # no dir segment


def test_curated_result_is_exempt_from_decay(tmp_path, monkeypatch):
    import memento.search as search

    monkeypatch.setattr(search, "get_vault", lambda: tmp_path)
    # Only the non-curated note reaches read_note_metadata / durability_tier.
    monkeypatch.setattr(search, "read_note_metadata", lambda name: {"certainty": 1, "date": "2000-01-01T00:00"})
    monkeypatch.setattr(search, "read_durability_tier", lambda *a, **k: "cold")

    results = [
        {"path": "profile/voice.md", "score": 1.0, "backend": "x"},
        {"path": "notes/old.md", "score": 1.0, "backend": "x"},
    ]
    out = search.apply_temporal_decay(results, config=dict(config.DEFAULT_CONFIG))
    by_path = {r["path"]: r for r in out}

    # Curated profile note: score untouched, tagged with the curated tier.
    assert by_path["profile/voice.md"]["score"] == 1.0
    assert by_path["profile/voice.md"]["_durability_tier"] == "curated"
    # A cold, ancient regular note decays.
    assert by_path["notes/old.md"]["score"] < 1.0


def test_curated_path_never_a_sweep_candidate(tmp_path, monkeypatch):
    import memento.archive as archive

    (tmp_path / "notes").mkdir(parents=True)
    # Inject a curated path into the sweep scan to exercise the guard directly
    # (_iter_vault_notes only scans notes/ today, so the guard is defensive).
    monkeypatch.setattr(archive, "_iter_vault_notes", lambda vault: iter(["profile/voice.md"]))

    cfg = dict(config.DEFAULT_CONFIG)
    cfg["archive_sweep_enabled"] = True

    report = archive.sweep_archive_candidates(tmp_path, config=cfg, dry_run=True)

    assert report["candidates"] == []
    assert {"path": "profile/voice.md", "reason": "curated (exempt)"} in report["skipped"]
