"""Tests for scripts/fix_certainty_values.py (MEM-150).

One-shot fixer for pre-existing out-of-range certainty values (e.g. the
95/97 typos found in the real vault, presumably meant to be 5). Dry-run by
default; --apply is exercised here only against tmp vaults, never the real
one.
"""

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_fixer():
    path = Path(__file__).parent.parent / "scripts" / "fix_certainty_values.py"
    spec = importlib.util.spec_from_file_location("fix_certainty_values", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fixer():
    return _load_fixer()


def _write_note(vault, stem, certainty_line):
    note = vault / "notes" / f"{stem}.md"
    note.write_text(
        f"---\ntitle: {stem}\ntype: discovery\ntags: [x]\n{certainty_line}date: 2026-01-01T00:00\n---\n\nBody.\n"
    )
    return note


class TestFindBadCertaintyNotes:
    def test_finds_out_of_range_notes_only(self, fixer, tmp_vault):
        _write_note(tmp_vault, "bad-95", "certainty: 95\n")
        _write_note(tmp_vault, "bad-97", "certainty: 97\n")
        _write_note(tmp_vault, "bad-0", "certainty: 0\n")
        _write_note(tmp_vault, "good", "certainty: 4\n")
        _write_note(tmp_vault, "no-certainty", "")

        offenders = list(fixer.find_bad_certainty_notes(tmp_vault))
        by_stem = {p.stem: (current, clamped) for p, current, clamped in offenders}

        assert by_stem == {
            "bad-95": (95, 5),
            "bad-97": (97, 5),
            "bad-0": (0, 1),
        }

    def test_empty_vault_yields_nothing(self, fixer, tmp_vault):
        assert list(fixer.find_bad_certainty_notes(tmp_vault)) == []

    def test_missing_notes_dir_yields_nothing(self, fixer, tmp_path):
        assert list(fixer.find_bad_certainty_notes(tmp_path / "does-not-exist")) == []


class TestFixNote:
    def test_apply_clamps_and_preserves_everything_else(self, fixer, tmp_vault):
        note = _write_note(tmp_vault, "bad-95", "certainty: 95\n")
        original = note.read_text()

        changed = fixer.fix_note(note, 5)

        assert changed is True
        new_text = note.read_text()
        assert "certainty: 5" in new_text
        assert "certainty: 95" not in new_text
        # Every other line is byte-for-byte unchanged.
        assert new_text == original.replace("certainty: 95", "certainty: 5")

    def test_fix_note_is_idempotent_when_already_clamped(self, fixer, tmp_vault):
        note = _write_note(tmp_vault, "already-fine", "certainty: 5\n")

        changed = fixer.fix_note(note, 5)

        assert changed is False


class TestMainDryRunVsApply:
    def test_dry_run_by_default_writes_nothing(self, fixer, tmp_vault, capsys):
        note = _write_note(tmp_vault, "bad-95", "certainty: 95\n")

        exit_code = fixer.main(["--vault", str(tmp_vault)])

        assert exit_code == 0
        assert "certainty: 95" in note.read_text()
        out = capsys.readouterr().out
        assert "bad-95" in out
        assert "95 -> 5" in out
        assert "Dry run only" in out

    def test_apply_writes_fixes_on_tmp_vault(self, fixer, tmp_vault, capsys):
        note = _write_note(tmp_vault, "bad-97", "certainty: 97\n")

        exit_code = fixer.main(["--vault", str(tmp_vault), "--apply"])

        assert exit_code == 0
        assert "certainty: 5" in note.read_text()
        assert "certainty: 97" not in note.read_text()
        out = capsys.readouterr().out
        assert "Fixed 1/1" in out

    def test_main_reports_clean_vault(self, fixer, tmp_vault, capsys):
        _write_note(tmp_vault, "good", "certainty: 3\n")

        exit_code = fixer.main(["--vault", str(tmp_vault)])

        assert exit_code == 0
        assert "No out-of-range certainty values found" in capsys.readouterr().out
