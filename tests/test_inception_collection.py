"""Tests for Inception note collection and filtering."""

import struct


def _write_note(path, *, title, note_type="discovery", tags=None, date="",
                certainty=None, project=None, source=None,
                synthesized_from=None, body=""):
    """Helper: write a markdown note with YAML frontmatter."""
    lines = ["---"]
    lines.append(f"title: {title}")
    lines.append(f"type: {note_type}")
    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    else:
        lines.append("tags: []")
    if date:
        lines.append(f"date: {date}")
    if certainty is not None:
        lines.append(f"certainty: {certainty}")
    if project:
        lines.append(f"project: {project}")
    if source:
        lines.append(f"source: {source}")
    if synthesized_from:
        lines.append("synthesized_from:")
        for s in synthesized_from:
            lines.append(f"  - {s}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")


class TestCollectEligibleNotes:
    """Tests for collect_eligible_notes filtering logic."""

    def test_collect_from_notes_only(self, tmp_vault, mock_config):
        """Only files in notes/ are collected; archive/ is ignored."""
        from memento_inception import collect_eligible_notes

        _write_note(tmp_vault / "notes" / "alpha.md", title="Alpha", date="2026-03-20T10:00")
        _write_note(tmp_vault / "archive" / "beta.md", title="Beta", date="2026-03-20T10:00")

        state = {"processed_notes": [], "last_run_iso": None}
        result = collect_eligible_notes(mock_config, state)

        stems = [r.stem for r in result]
        assert "alpha" in stems
        assert "beta" not in stems

    def test_skip_inception_source(self, tmp_vault, mock_config):
        """Notes with source: inception are excluded to prevent recursion."""
        from memento_inception import collect_eligible_notes

        _write_note(tmp_vault / "notes" / "human-note.md", title="Human note",
                     date="2026-03-20T10:00")
        _write_note(tmp_vault / "notes" / "inception-generated.md", title="Inception output",
                     date="2026-03-20T10:00", source="inception")

        state = {"processed_notes": [], "last_run_iso": None}
        result = collect_eligible_notes(mock_config, state)

        stems = [r.stem for r in result]
        assert "human-note" in stems
        assert "inception-generated" not in stems

    def test_skip_excluded_tags(self, tmp_vault, mock_config):
        """Notes matching inception_exclude_tags are excluded."""
        from memento_inception import collect_eligible_notes

        mock_config["inception_exclude_tags"] = ["private", "draft"]
        _write_note(tmp_vault / "notes" / "public.md", title="Public",
                     tags=["redis"], date="2026-03-20T10:00")
        _write_note(tmp_vault / "notes" / "secret.md", title="Secret",
                     tags=["private", "redis"], date="2026-03-20T10:00")
        _write_note(tmp_vault / "notes" / "wip.md", title="WIP",
                     tags=["draft"], date="2026-03-20T10:00")

        state = {"processed_notes": [], "last_run_iso": None}
        result = collect_eligible_notes(mock_config, state)

        stems = [r.stem for r in result]
        assert "public" in stems
        assert "secret" not in stems
        assert "wip" not in stems

    def test_skip_processed(self, tmp_vault, mock_config):
        """Already-processed notes are skipped in incremental mode."""
        from memento_inception import collect_eligible_notes

        _write_note(tmp_vault / "notes" / "done.md", title="Done",
                     date="2026-03-20T10:00")
        _write_note(tmp_vault / "notes" / "fresh.md", title="Fresh",
                     date="2026-03-20T10:00")

        state = {"processed_notes": ["done"], "last_run_iso": None}
        result = collect_eligible_notes(mock_config, state)

        stems = [r.stem for r in result]
        assert "fresh" in stems
        assert "done" not in stems

    def test_full_ignores_processed(self, tmp_vault, mock_config):
        """With full=True, processed notes are still included."""
        from memento_inception import collect_eligible_notes

        _write_note(tmp_vault / "notes" / "done.md", title="Done",
                     date="2026-03-20T10:00")

        state = {"processed_notes": ["done"], "last_run_iso": None}
        result = collect_eligible_notes(mock_config, state, full=True)

        stems = [r.stem for r in result]
        assert "done" in stems

    def test_incremental_date_filter(self, tmp_vault, mock_config):
        """Only notes newer than last_run_iso are included in incremental runs."""
        from memento_inception import collect_eligible_notes

        _write_note(tmp_vault / "notes" / "old-note.md", title="Old",
                     date="2026-03-10T08:00")
        _write_note(tmp_vault / "notes" / "new-note.md", title="New",
                     date="2026-03-20T14:00")

        state = {"processed_notes": [], "last_run_iso": "2026-03-15T00:00"}
        result = collect_eligible_notes(mock_config, state)

        stems = [r.stem for r in result]
        assert "new-note" in stems
        assert "old-note" not in stems

    def test_skip_dotfiles(self, tmp_vault, mock_config):
        """Hidden files (starting with .) are skipped."""
        from memento_inception import collect_eligible_notes

        _write_note(tmp_vault / "notes" / ".tmp-write.md", title="Temp")
        _write_note(tmp_vault / "notes" / "visible.md", title="Visible",
                     date="2026-03-20T10:00")

        state = {"processed_notes": [], "last_run_iso": None}
        result = collect_eligible_notes(mock_config, state)

        stems = [r.stem for r in result]
        assert "visible" in stems
        assert ".tmp-write" not in stems


class TestParseNote:
    """Tests for parse_note on individual note files."""

    def test_parse_note_valid(self, tmp_vault):
        """Well-formed note parses all frontmatter fields correctly."""
        from memento_inception import parse_note

        path = tmp_vault / "notes" / "test-note.md"
        _write_note(path, title="Test Title", note_type="discovery",
                     tags=["redis", "caching"], date="2026-03-15T09:00",
                     certainty=4, project="/home/vic/Projects/api",
                     body="Body text with [[wikilink-target]] inside.")

        record = parse_note(path)

        assert record is not None
        assert record.stem == "test-note"
        assert record.title == "Test Title"
        assert record.note_type == "discovery"
        assert record.tags == ["redis", "caching"]
        assert record.date == "2026-03-15T09:00"
        assert record.certainty == 4
        assert record.project == "/home/vic/Projects/api"
        assert "wikilink-target" in record.wikilinks
        assert "Body text" in record.body

    def test_parse_note_missing_frontmatter(self, tmp_vault):
        """Note without --- frontmatter delimiters is parsed gracefully."""
        from memento_inception import parse_note

        path = tmp_vault / "notes" / "no-fm.md"
        path.write_text("Just a plain markdown file.\nNo frontmatter here.", encoding="utf-8")

        record = parse_note(path)

        assert record is not None
        assert record.stem == "no-fm"
        assert record.title == "no-fm"  # falls back to stem
        assert record.note_type == "unknown"
        assert record.tags == []
        assert record.body == "Just a plain markdown file.\nNo frontmatter here."

    def test_parse_note_binary_file(self, tmp_vault):
        """Non-text (binary) file returns None."""
        from memento_inception import parse_note

        path = tmp_vault / "notes" / "binary.md"
        # Write invalid UTF-8 bytes
        path.write_bytes(b"\x80\x81\x82\xff\xfe" + struct.pack("f" * 10, *range(10)))

        record = parse_note(path)
        assert record is None
