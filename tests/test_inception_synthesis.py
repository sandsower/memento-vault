"""Tests for Inception synthesis prompt builder."""

from pathlib import Path

from memento_inception import NoteRecord, build_synthesis_prompt


def _make_note(stem, title="Untitled", note_type="discovery", tags=None,
               date="2026-03-10T14:00", certainty=4, project="/home/vic/Projects/api",
               body="Some body text."):
    """Helper to build a NoteRecord without touching the filesystem."""
    return NoteRecord(
        stem=stem,
        path=Path(f"/fake/notes/{stem}.md"),
        title=title,
        note_type=note_type,
        tags=tags or [],
        date=date,
        certainty=certainty,
        project=project,
        body=body,
    )


class TestBuildSynthesisPrompt:
    """Tests for build_synthesis_prompt."""

    def test_prompt_includes_all_sources(self):
        """All source note titles appear in the generated prompt."""
        notes = {
            "note-a": _make_note("note-a", title="Alpha insight"),
            "note-b": _make_note("note-b", title="Beta discovery"),
            "note-c": _make_note("note-c", title="Gamma pattern"),
        }
        result = build_synthesis_prompt(["note-a", "note-b", "note-c"], notes)

        assert "Alpha insight" in result
        assert "Beta discovery" in result
        assert "Gamma pattern" in result

    def test_prompt_includes_system_instructions(self):
        """Prompt contains the Inception identity and the SKIP instruction."""
        notes = {
            "x": _make_note("x", title="Anything"),
        }
        result = build_synthesis_prompt(["x"], notes)

        assert "Inception" in result
        assert "SKIP" in result

    def test_prompt_includes_note_metadata(self):
        """Tags, date, certainty, and project are present for each source note."""
        notes = {
            "m": _make_note(
                "m",
                title="Meta note",
                tags=["redis", "caching"],
                date="2026-03-15T09:00",
                certainty=3,
                project="/home/vic/Projects/billing",
            ),
        }
        result = build_synthesis_prompt(["m"], notes)

        assert "redis" in result
        assert "caching" in result
        assert "2026-03-15T09:00" in result
        assert "3" in result
        assert "/home/vic/Projects/billing" in result

    def test_prompt_merge_target(self):
        """When merge_target is provided, merge instructions appear."""
        notes = {
            "a": _make_note("a", title="A note"),
        }
        result = build_synthesis_prompt(["a"], notes, merge_target="existing-pattern")

        assert "[[existing-pattern]]" in result
        assert "Revise and expand" in result

    def test_prompt_no_merge_target(self):
        """Without merge_target, no merge instructions in the output."""
        notes = {
            "a": _make_note("a", title="A note"),
        }
        result = build_synthesis_prompt(["a"], notes)

        assert "Revise and expand" not in result
        assert "existing pattern note" not in result

    def test_prompt_handles_missing_stem(self):
        """A stem not in notes_dict is silently skipped; other notes still appear."""
        notes = {
            "present": _make_note("present", title="I exist"),
        }
        result = build_synthesis_prompt(["present", "ghost"], notes)

        assert "I exist" in result
        assert "ghost" not in result

    def test_prompt_handles_missing_fields(self):
        """Note with None certainty and None project produces valid output."""
        notes = {
            "sparse": _make_note(
                "sparse",
                title="Sparse note",
                certainty=None,
                project=None,
            ),
        }
        result = build_synthesis_prompt(["sparse"], notes)

        assert "Sparse note" in result
        assert "?" in result      # certainty falls back to '?'
        assert "none" in result   # project falls back to 'none'
