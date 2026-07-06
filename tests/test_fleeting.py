"""Tests for the fleeting note lifecycle sweep (MEM-153).

Covers memento.archive.fleeting_lifecycle_sweep (promotion-by-resurfacing,
promotion-by-citation, expiry, gating, reporting shape) and
memento.archive.promote_fleeting_note (the reusable move+frontmatter-stamp
primitive it builds on). Reuses archive_note/restore_note (MEM-152) for
expiry -- test_archive.py covers those primitives directly; this file only
adds the MEM-153 fleeting-specific behavior on top.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import memento.archive as archive_mod
from memento.archive import (
    ArchiveError,
    _fleeting_age_days,
    fleeting_lifecycle_sweep,
    latest_active_tombstones,
    promote_fleeting_note,
    restore_note,
)

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_vault_write_lock(tmp_path, monkeypatch):
    """MEM-153 mutates fleeting/notes/archive -- keep the write lock off the
    real ~/.cache/memento-vault so this never races other worktrees' sweeps."""
    monkeypatch.setattr("memento.store.VAULT_WRITE_LOCK_PATH", str(tmp_path / "vault-write.lock"))


def _config(**overrides) -> dict:
    config = {
        "fleeting_lifecycle_enabled": True,
        "fleeting_promote_min_resurfaced": 2,
        "fleeting_expire_days": 14,
    }
    config.update(overrides)
    return config


def _write_fleeting(vault: Path, name: str, *, frontmatter_lines=None, body: str = "# fleeting\n\ncontent\n") -> Path:
    path = vault / "fleeting" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if frontmatter_lines:
        text = "---\n" + "\n".join(frontmatter_lines) + "\n---\n" + body
    else:
        text = body
    path.write_text(text, encoding="utf-8")
    return path


def _write_session_summary(vault: Path, rel: str, *, cites: str | None = None, source: str = "mcp-capture") -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "Session summary body."
    if cites:
        body += f"\n\nSee [[{cites}]] for the fleeting log.\n"
    text = f"---\ntitle: Session\ntype: discovery\ntags: []\nsource: {source}\n---\n\n{body}\n"
    path.write_text(text, encoding="utf-8")
    return path


def _set_mtime(path: Path, days_ago: float) -> None:
    ts = (NOW - timedelta(days=days_ago)).timestamp()
    os.utime(path, (ts, ts))


class TestPromotionViaResurfacedCount:
    def test_promotes_when_resurfaced_count_meets_threshold(self, tmp_vault):
        _write_fleeting(tmp_vault, "2026-05-19.md", frontmatter_lines=["resurfaced_count: 2"])

        report = fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW)

        assert [p["path"] for p in report["promoted"]] == ["fleeting/2026-05-19.md"]
        assert report["promoted"][0]["reason"] == "resurfaced_count >= 2"
        assert report["promoted"][0]["notes_path"] == "notes/2026-05-19.md"
        assert not (tmp_vault / "fleeting" / "2026-05-19.md").exists()
        promoted = tmp_vault / "notes" / "2026-05-19.md"
        assert promoted.exists()
        assert "promoted_at: 2026-07-06" in promoted.read_text()
        assert "resurfaced_count: 2" in promoted.read_text()

    def test_below_threshold_uses_default_config_value(self, tmp_vault):
        path = _write_fleeting(tmp_vault, "2026-07-01.md", frontmatter_lines=["resurfaced_count: 1"])
        _set_mtime(path, 5)  # recent -- not promoted, not expired

        report = fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW)

        assert report["promoted"] == []
        assert report["expired"] == []
        assert path.exists()


class TestPromotionViaCitation:
    def test_promotes_when_cited_by_session_summary_note(self, tmp_vault):
        _write_fleeting(tmp_vault, "2026-05-19.md")
        _write_session_summary(tmp_vault, "notes/summary.md", cites="2026-05-19")

        report = fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW)

        assert [p["path"] for p in report["promoted"]] == ["fleeting/2026-05-19.md"]
        assert report["promoted"][0]["reason"] == "cited by session-summary note"
        assert (tmp_vault / "notes" / "2026-05-19.md").exists()

    def test_citation_from_non_session_summary_source_does_not_promote(self, tmp_vault):
        path = _write_fleeting(tmp_vault, "2026-07-01.md")
        _set_mtime(path, 5)  # recent -- would only be promoted, never expired here
        _write_session_summary(tmp_vault, "notes/other.md", cites="2026-07-01", source="session")

        report = fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW)

        assert report["promoted"] == []
        assert path.exists()

    def test_section_link_is_not_treated_as_a_citation(self, tmp_vault):
        path = _write_fleeting(tmp_vault, "2026-07-01.md")
        _set_mtime(path, 5)
        _write_session_summary(tmp_vault, "notes/summary.md", cites="2026-07-01#sessions", source="mcp-capture")

        report = fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW)

        assert report["promoted"] == []
        assert path.exists()


class TestUntouchedBelowThresholds:
    def test_recent_uncited_low_count_note_is_left_alone(self, tmp_vault):
        path = _write_fleeting(tmp_vault, "2026-07-01.md", frontmatter_lines=["resurfaced_count: 1"])
        _set_mtime(path, 5)

        report = fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW)

        assert report["promoted"] == []
        assert report["expired"] == []
        assert report["skipped"] == []
        assert path.exists()


class TestExpiry:
    def test_expires_old_uncited_note_with_tombstone(self, tmp_vault):
        path = _write_fleeting(tmp_vault, "2026-06-01.md")
        _set_mtime(path, 20)

        report = fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW)

        assert [e["path"] for e in report["expired"]] == ["fleeting/2026-06-01.md"]
        assert report["expired"][0]["archive_path"] == "archive/fleeting/2026-06-01.md"
        assert not path.exists()
        archived = tmp_vault / "archive" / "fleeting" / "2026-06-01.md"
        assert archived.exists()
        assert "fleeting/2026-06-01.md" in latest_active_tombstones(tmp_vault)

    def test_expired_note_is_reversible_via_restore_note(self, tmp_vault):
        path = _write_fleeting(tmp_vault, "2026-06-01.md", body="# 2026-06-01\n\ncontent\n")
        original = path.read_text()
        _set_mtime(path, 20)

        fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW)
        restore_note(tmp_vault, "fleeting/2026-06-01.md")

        assert path.exists()
        assert path.read_text() == original
        assert "fleeting/2026-06-01.md" not in latest_active_tombstones(tmp_vault)

    def test_age_just_under_the_threshold_is_not_expired(self, tmp_vault):
        path = _write_fleeting(tmp_vault, "2026-06-22.md")
        _set_mtime(path, 13.5)  # comfortably under the 14-day default, avoids float-timestamp edge flakiness

        report = fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW)

        assert report["expired"] == []
        assert path.exists()

    def test_date_frontmatter_takes_precedence_over_mtime(self, tmp_vault):
        # date says old (should expire); mtime says fresh (would not expire alone).
        path = _write_fleeting(tmp_vault, "2026-06-01.md", frontmatter_lines=["date: 2026-06-01"])
        _set_mtime(path, 1)

        report = fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW)

        assert [e["path"] for e in report["expired"]] == ["fleeting/2026-06-01.md"]


class TestMissingAgeSignals:
    def test_fleeting_age_days_returns_none_when_stat_fails(self, tmp_path):
        missing = tmp_path / "gone.md"  # never created -> stat() raises OSError
        assert _fleeting_age_days(missing, "", NOW) is None

    def test_sweep_skips_with_reason_when_age_is_unresolvable(self, tmp_vault, monkeypatch):
        path = _write_fleeting(tmp_vault, "2026-06-01.md")
        monkeypatch.setattr(archive_mod, "_fleeting_age_days", lambda p, f, n: None)

        report = fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW)

        assert report["promoted"] == []
        assert report["expired"] == []
        assert report["skipped"] == [
            {"path": "fleeting/2026-06-01.md", "reason": "no date frontmatter and no readable mtime"}
        ]
        assert path.exists()


class TestGating:
    def test_disabled_is_a_noop_and_does_not_scan(self, tmp_vault, capsys):
        _write_fleeting(tmp_vault, "2026-05-19.md")

        report = fleeting_lifecycle_sweep(tmp_vault, config=_config(fleeting_lifecycle_enabled=False), now=NOW)

        assert report["enabled"] is False
        assert report["promoted"] == []
        assert report["expired"] == []
        assert (tmp_vault / "fleeting" / "2026-05-19.md").exists()
        stderr = capsys.readouterr().err
        assert "fleeting_lifecycle_enabled" in stderr
        assert "disabled" in stderr

    def test_default_config_values_are_used_when_omitted(self, tmp_vault):
        _write_fleeting(tmp_vault, "2026-05-19.md")

        # No fleeting_* keys at all -- must default to disabled (safe default).
        report = fleeting_lifecycle_sweep(tmp_vault, config={}, now=NOW)

        assert report["enabled"] is False
        assert report["promoted"] == []
        assert (tmp_vault / "fleeting" / "2026-05-19.md").exists()

    def test_dry_run_reports_but_mutates_nothing(self, tmp_vault):
        promotable = _write_fleeting(tmp_vault, "2026-05-19.md", frontmatter_lines=["resurfaced_count: 5"])
        expirable = _write_fleeting(tmp_vault, "2026-06-01.md")
        _set_mtime(expirable, 20)

        report = fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW, dry_run=True)

        assert report["dry_run"] is True
        assert [p["path"] for p in report["promoted"]] == ["fleeting/2026-05-19.md"]
        assert [e["path"] for e in report["expired"]] == ["fleeting/2026-06-01.md"]
        assert promotable.exists()
        assert expirable.exists()
        assert not (tmp_vault / "notes" / "2026-05-19.md").exists()
        assert not (tmp_vault / "archive" / "fleeting" / "2026-06-01.md").exists()
        assert "fleeting/2026-06-01.md" not in latest_active_tombstones(tmp_vault)


class TestPromotionPreservesFrontmatter:
    def test_promotion_preserves_unknown_frontmatter_keys(self, tmp_vault):
        _write_fleeting(
            tmp_vault,
            "2026-05-19.md",
            frontmatter_lines=["resurfaced_count: 2", "custom_key: keep-me", "another: 42"],
            body="# 2026-05-19\n\nBody text.\n",
        )

        fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW)

        text = (tmp_vault / "notes" / "2026-05-19.md").read_text()
        assert "custom_key: keep-me" in text
        assert "another: 42" in text
        assert "resurfaced_count: 2" in text
        assert "promoted_at: 2026-07-06" in text
        assert "Body text." in text

    def test_promotion_of_frontmatter_less_note_adds_only_promoted_at(self, tmp_vault):
        # Matches the real memento.store.append_fleeting_session shape: no
        # YAML frontmatter block at all, promoted here via citation (not
        # resurfaced_count, which needs a frontmatter block to live in).
        _write_fleeting(tmp_vault, "2026-05-19.md", body="# 2026-05-19\n\n- a session\n")
        _write_session_summary(tmp_vault, "notes/summary.md", cites="2026-05-19")

        fleeting_lifecycle_sweep(tmp_vault, config=_config(), now=NOW)

        text = (tmp_vault / "notes" / "2026-05-19.md").read_text()
        assert text.startswith("---\npromoted_at: 2026-07-06\n---\n")
        assert "- a session" in text


class TestPromoteFleetingNoteHelper:
    def test_avoids_collision_with_existing_notes_file(self, tmp_vault):
        (tmp_vault / "notes" / "2026-05-19.md").write_text("existing note\n", encoding="utf-8")
        _write_fleeting(tmp_vault, "2026-05-19.md")

        result = promote_fleeting_note(tmp_vault, "fleeting/2026-05-19.md", now=NOW)

        assert result["notes_path"] == "notes/2026-05-19-2.md"
        assert (tmp_vault / "notes" / "2026-05-19-2.md").exists()
        assert (tmp_vault / "notes" / "2026-05-19.md").read_text() == "existing note\n"

    def test_missing_file_raises(self, tmp_vault):
        with pytest.raises(ArchiveError):
            promote_fleeting_note(tmp_vault, "fleeting/missing.md")
