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


def test_check_profile_absent_is_pass(tmp_path):
    from memento.health import _check_profile

    (tmp_path / "notes").mkdir(parents=True)  # vault without profile/
    result = _check_profile(tmp_path, config.DEFAULT_CONFIG)
    assert result.status == "pass"
    assert result.details.get("present") is False


def test_check_profile_present_reports_facets(tmp_path):
    from memento.health import _check_profile

    pdir = tmp_path / "profile"
    pdir.mkdir(parents=True)
    (pdir / "PROFILE.md").write_text("# index\n\n- voice")
    (pdir / "README.md").write_text("docs")
    (pdir / "voice.md").write_text("---\nname: voice\n---\n")
    (pdir / "identity.md").write_text("---\nname: identity\n---\n")

    result = _check_profile(tmp_path, config.DEFAULT_CONFIG)
    assert result.status == "pass"
    assert result.details["facets"] == 2  # PROFILE.md + README.md excluded
    assert result.details["index_present"] is True


def test_check_profile_facets_without_index_warns(tmp_path):
    from memento.health import _check_profile

    pdir = tmp_path / "profile"
    pdir.mkdir(parents=True)
    (pdir / "voice.md").write_text("---\nname: voice\n---\n")

    result = _check_profile(tmp_path, config.DEFAULT_CONFIG)
    assert result.status == "warn"


def test_index_staleness_scans_profile_dir(tmp_path):
    import os
    import time

    from memento.health import _embedded_index_staleness

    (tmp_path / "notes").mkdir(parents=True)
    (tmp_path / "profile").mkdir(parents=True)
    db = tmp_path / ".search" / "search.db"
    db.parent.mkdir(parents=True)
    db.write_text("x")
    # Age the db an hour; a fresh profile edit must read as stale.
    old = time.time() - 3600
    os.utime(db, (old, old))
    (tmp_path / "profile" / "voice.md").write_text("---\nname: voice\n---\n")

    meta = _embedded_index_staleness(tmp_path, {"search_db_path": ".search/search.db"})
    assert meta["stale"] is True
    assert meta["status"] == "warn"
