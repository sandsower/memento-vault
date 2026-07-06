"""Tests for the reversible note archive sweep (MEM-152).

Covers memento.archive.archive_note/restore_note (the reusable move+tombstone
primitives) and sweep_archive_candidates (the criteria matrix, gating knobs,
and reporting shape). test_archive_portable.py covers the pre-existing
portable export/import machinery these primitives build on -- this file only
adds the MEM-152 sweep on top of it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from memento.archive import (
    ARCHIVE_SWEEP_CERTAINTY_CEILING,
    ArchiveError,
    archive_note,
    latest_active_tombstones,
    restore_note,
    sweep_archive_candidates,
    tombstones_path,
)

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
OLD_DATE = "2026-01-01T00:00"  # ~186 days before NOW
RECENT_DATE = "2026-06-20T00:00"  # ~16 days before NOW


def _write_note(
    vault: Path,
    rel: str,
    *,
    date: str | None = OLD_DATE,
    certainty: int | None = 2,
    pinned: bool | None = None,
    resurfaced_count: int | None = None,
    last_resurfaced: str | None = None,
    body: str = "Body.",
) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", "title: Test note", "type: discovery", "tags: []"]
    if certainty is not None:
        lines.append(f"certainty: {certainty}")
    if pinned is not None:
        lines.append(f"pinned: {'true' if pinned else 'false'}")
    if date is not None:
        lines.append(f"date: {date}")
    if resurfaced_count is not None:
        lines.append(f"resurfaced_count: {resurfaced_count}")
    if last_resurfaced is not None:
        lines.append(f"last_resurfaced: {last_resurfaced}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _config(**overrides) -> dict:
    config = {
        "archive_sweep_enabled": True,
        "archive_sweep_age_days": 90,
        "archive_sweep_max_per_run": 50,
        "durability_hot_window_days": 30,
    }
    config.update(overrides)
    return config


class TestArchiveNoteRestoreNote:
    """The reusable move+tombstone primitives the sweep builds on."""

    def test_archive_note_moves_file_and_records_tombstone(self, tmp_vault):
        note = _write_note(tmp_vault, "notes/example.md")
        original_text = note.read_text()

        result = archive_note(tmp_vault, "notes/example.md")

        assert result["archive_path"] == "archive/example.md"
        assert not note.exists()
        archived = tmp_vault / "archive" / "example.md"
        assert archived.read_text() == original_text
        assert "notes/example.md" in tombstones_path(tmp_vault).read_text()
        assert "notes/example.md" in latest_active_tombstones(tmp_vault)

    def test_archive_note_missing_file_raises(self, tmp_vault):
        with pytest.raises(ArchiveError):
            archive_note(tmp_vault, "notes/missing.md")

    def test_restore_note_reverses_archive_note(self, tmp_vault):
        note = _write_note(tmp_vault, "notes/example.md")
        original_text = note.read_text()
        archive_note(tmp_vault, "notes/example.md")

        result = restore_note(tmp_vault, "notes/example.md")

        assert result["path"] == "notes/example.md"
        assert note.exists()
        assert note.read_text() == original_text
        assert not (tmp_vault / "archive" / "example.md").exists()
        # The restore is itself an appended tombstone record (reason=restored),
        # never a rewrite/deletion of the archived-tombstone history.
        assert "notes/example.md" not in latest_active_tombstones(tmp_vault)

    def test_restore_note_missing_archived_file_raises(self, tmp_vault):
        with pytest.raises(ArchiveError):
            restore_note(tmp_vault, "notes/never-archived.md")


class TestSweepCriteriaMatrix:
    """Each of the three criteria must independently gate archiving."""

    def test_all_three_criteria_pass_archives_with_tombstone(self, tmp_vault):
        _write_note(tmp_vault, "notes/cold-old-low.md", date=OLD_DATE, certainty=2)

        report = sweep_archive_candidates(tmp_vault, config=_config(), now=NOW)

        assert report["candidates"] == [
            {"path": "notes/cold-old-low.md", "tier": "cold", "age_days": 186.5, "certainty": 2}
        ]
        assert [a["path"] for a in report["archived"]] == ["notes/cold-old-low.md"]
        assert report["skipped"] == []
        assert not (tmp_vault / "notes" / "cold-old-low.md").exists()
        assert (tmp_vault / "archive" / "cold-old-low.md").exists()
        assert "notes/cold-old-low.md" in latest_active_tombstones(tmp_vault)

    def test_pinned_tier_excluded_even_when_old_and_low_certainty(self, tmp_vault):
        _write_note(tmp_vault, "notes/pinned.md", date=OLD_DATE, certainty=2, pinned=True)

        report = sweep_archive_candidates(tmp_vault, config=_config(), now=NOW)

        assert report["candidates"] == []
        assert report["archived"] == []
        assert (tmp_vault / "notes" / "pinned.md").exists()

    def test_hot_tier_excluded_even_when_old_and_low_certainty(self, tmp_vault):
        _write_note(
            tmp_vault,
            "notes/hot.md",
            date=OLD_DATE,
            certainty=2,
            resurfaced_count=3,
            last_resurfaced="2026-06-20T00:00:00Z",  # 16 days ago -> within hot window
        )

        report = sweep_archive_candidates(tmp_vault, config=_config(), now=NOW)

        assert report["candidates"] == []
        assert (tmp_vault / "notes" / "hot.md").exists()

    def test_warm_tier_excluded_even_when_old_and_low_certainty(self, tmp_vault):
        _write_note(
            tmp_vault,
            "notes/warm.md",
            date=OLD_DATE,
            certainty=2,
            resurfaced_count=3,
            last_resurfaced="2026-01-01T00:00:00Z",  # long ago -> resurfaced but not hot
        )

        report = sweep_archive_candidates(tmp_vault, config=_config(), now=NOW)

        assert report["candidates"] == []
        assert (tmp_vault / "notes" / "warm.md").exists()

    def test_age_not_exceeded_excludes_cold_low_certainty_note(self, tmp_vault):
        _write_note(tmp_vault, "notes/recent.md", date=RECENT_DATE, certainty=2)

        report = sweep_archive_candidates(tmp_vault, config=_config(), now=NOW)

        assert report["candidates"] == []
        assert (tmp_vault / "notes" / "recent.md").exists()

    def test_missing_date_fails_safe_and_excludes(self, tmp_vault):
        _write_note(tmp_vault, "notes/undated.md", date=None, certainty=2)

        report = sweep_archive_candidates(tmp_vault, config=_config(), now=NOW)

        assert report["candidates"] == []
        assert (tmp_vault / "notes" / "undated.md").exists()

    def test_certainty_at_ceiling_excludes_cold_old_note(self, tmp_vault):
        _write_note(tmp_vault, "notes/shipped.md", date=OLD_DATE, certainty=ARCHIVE_SWEEP_CERTAINTY_CEILING)

        report = sweep_archive_candidates(tmp_vault, config=_config(), now=NOW)

        assert report["candidates"] == []
        assert (tmp_vault / "notes" / "shipped.md").exists()

    def test_missing_certainty_fails_safe_and_excludes(self, tmp_vault):
        _write_note(tmp_vault, "notes/no-certainty.md", date=OLD_DATE, certainty=None)

        report = sweep_archive_candidates(tmp_vault, config=_config(), now=NOW)

        assert report["candidates"] == []
        assert (tmp_vault / "notes" / "no-certainty.md").exists()


class TestSweepGating:
    def test_disabled_is_a_noop_and_does_not_scan(self, tmp_vault, capsys):
        _write_note(tmp_vault, "notes/cold-old-low.md", date=OLD_DATE, certainty=2)

        report = sweep_archive_candidates(tmp_vault, config=_config(archive_sweep_enabled=False), now=NOW)

        assert report["enabled"] is False
        assert report["candidates"] == []
        assert report["archived"] == []
        assert (tmp_vault / "notes" / "cold-old-low.md").exists()
        stderr = capsys.readouterr().err
        assert "archive_sweep_enabled" in stderr
        assert "disabled" in stderr

    def test_dry_run_reports_candidates_but_archives_nothing(self, tmp_vault):
        _write_note(tmp_vault, "notes/cold-old-low.md", date=OLD_DATE, certainty=2)

        report = sweep_archive_candidates(tmp_vault, config=_config(), now=NOW, dry_run=True)

        assert report["dry_run"] is True
        assert [c["path"] for c in report["candidates"]] == ["notes/cold-old-low.md"]
        assert report["archived"] == []
        assert (tmp_vault / "notes" / "cold-old-low.md").exists()
        assert not (tmp_vault / "archive" / "cold-old-low.md").exists()
        assert "notes/cold-old-low.md" not in latest_active_tombstones(tmp_vault)

    def test_max_per_run_caps_archiving_and_reports_overflow_as_skipped(self, tmp_vault):
        for i in range(3):
            _write_note(tmp_vault, f"notes/cold-old-low-{i}.md", date=OLD_DATE, certainty=2)

        report = sweep_archive_candidates(tmp_vault, config=_config(archive_sweep_max_per_run=2), now=NOW)

        assert len(report["candidates"]) == 3
        assert len(report["archived"]) == 2
        assert len(report["skipped"]) == 1
        assert "max_per_run" in report["skipped"][0]["reason"]
        remaining = sorted(p.name for p in (tmp_vault / "notes").glob("cold-old-low-*.md"))
        assert len(remaining) == 1
        archived_names = sorted(p.name for p in (tmp_vault / "archive").glob("cold-old-low-*.md"))
        assert len(archived_names) == 2

    def test_default_config_values_are_used_when_omitted(self, tmp_vault):
        _write_note(tmp_vault, "notes/cold-old-low.md", date=OLD_DATE, certainty=2)

        # No archive_sweep_* keys at all -- must default to disabled (safe default).
        report = sweep_archive_candidates(tmp_vault, config={}, now=NOW)

        assert report["enabled"] is False
        assert report["archived"] == []
        assert (tmp_vault / "notes" / "cold-old-low.md").exists()


class TestSweepReversibility:
    def test_archived_note_can_be_restored_via_restore_note(self, tmp_vault):
        note = _write_note(tmp_vault, "notes/cold-old-low.md", date=OLD_DATE, certainty=2)
        original_text = note.read_text()

        report = sweep_archive_candidates(tmp_vault, config=_config(), now=NOW)
        assert [a["path"] for a in report["archived"]] == ["notes/cold-old-low.md"]
        assert not note.exists()

        restore_note(tmp_vault, "notes/cold-old-low.md")

        assert note.exists()
        assert note.read_text() == original_text
        assert not (tmp_vault / "archive" / "cold-old-low.md").exists()
        assert "notes/cold-old-low.md" not in latest_active_tombstones(tmp_vault)
