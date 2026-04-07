"""Tests for write_pattern_note and backlink_sources in Inception."""

from pathlib import Path


def _write_note(
    path,
    *,
    title,
    note_type="discovery",
    tags=None,
    date="",
    certainty=None,
    project=None,
    source=None,
    synthesized_from=None,
    body="",
    related=None,
):
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

    if related is not None:
        lines.append("")
        lines.append("## Related")
        lines.append("")
        for r in related:
            lines.append(f"- [[{r}]]")

    path.write_text("\n".join(lines), encoding="utf-8")


class TestWritePatternNote:
    """Tests for write_pattern_note."""

    def _make_synthesis(self, **overrides):
        """Build a default synthesis dict, applying overrides."""
        synthesis = {
            "title": "Cross-project Redis caching strategies",
            "body": "Redis caching patterns recur across multiple services.\n\nUse explicit TTL and LRU eviction.",
            "tags": ["redis", "caching"],
            "certainty": 4,
            "related": ["redis-cache-ttl", "redis-eviction-policy"],
        }
        synthesis.update(overrides)
        return synthesis

    def test_writes_valid_pattern_note(self, tmp_vault):
        """Written note contains correct frontmatter fields."""
        from memento_inception import write_pattern_note

        synthesis = self._make_synthesis()
        cluster_stems = ["redis-cache-ttl", "redis-eviction-policy"]

        result = write_pattern_note(synthesis, cluster_stems, tmp_vault)

        assert result is not None
        assert result.exists()

        text = result.read_text()

        # Verify frontmatter fields
        assert "title: Cross-project Redis caching strategies" in text
        assert "type: pattern" in text
        assert "source: inception" in text
        assert "tags: [redis, caching]" in text
        assert "certainty: 3" in text  # inception caps certainty at 3
        assert "synthesized_from:" in text
        assert "  - redis-cache-ttl" in text
        assert "  - redis-eviction-policy" in text
        # date should be present (YYYY-MM-DDTHH:MM format)
        assert "date: 20" in text

    def test_atomic_write_no_temp_file(self, tmp_vault):
        """After write completes, no .inception-tmp-* files remain."""
        from memento_inception import write_pattern_note

        synthesis = self._make_synthesis()
        cluster_stems = ["redis-cache-ttl"]

        write_pattern_note(synthesis, cluster_stems, tmp_vault)

        notes_dir = tmp_vault / "notes"
        temp_files = list(notes_dir.glob(".inception-tmp-*"))
        assert temp_files == []

    def test_slug_collision(self, tmp_vault):
        """Second note with same title gets -2 suffix."""
        from memento_inception import write_pattern_note

        synthesis = self._make_synthesis()
        cluster_stems = ["redis-cache-ttl"]

        first = write_pattern_note(synthesis, cluster_stems, tmp_vault)
        second = write_pattern_note(synthesis, cluster_stems, tmp_vault)

        assert first is not None
        assert second is not None
        assert first != second
        assert "-2" in second.stem

    def test_body_and_related(self, tmp_vault):
        """Note body text and ## Related section with wikilinks are present."""
        from memento_inception import write_pattern_note

        synthesis = self._make_synthesis(
            body="Redis caching patterns recur across multiple services.",
            related=["redis-cache-ttl", "redis-eviction-policy"],
        )
        cluster_stems = ["redis-cache-ttl", "redis-eviction-policy"]

        result = write_pattern_note(synthesis, cluster_stems, tmp_vault)
        text = result.read_text()

        assert "Redis caching patterns recur across multiple services." in text
        assert "## Related" in text
        assert "- [[redis-cache-ttl]]" in text
        assert "- [[redis-eviction-policy]]" in text

    def test_returns_path(self, tmp_vault):
        """Function returns a Path to the written note."""
        from memento_inception import write_pattern_note

        synthesis = self._make_synthesis()
        cluster_stems = ["redis-cache-ttl"]

        result = write_pattern_note(synthesis, cluster_stems, tmp_vault)

        assert isinstance(result, Path)
        assert result.suffix == ".md"
        assert result.parent == tmp_vault / "notes"


class TestBacklinkSources:
    """Tests for backlink_sources."""

    def test_backlink_adds_to_related(self, tmp_vault):
        """Link appended to existing ## Related section."""
        from memento_inception import backlink_sources

        note_path = tmp_vault / "notes" / "redis-cache-ttl.md"
        _write_note(
            note_path,
            title="Redis cache requires explicit TTL",
            body="Setting explicit TTL on Redis keys.",
            related=["some-other-note"],
        )

        backlink_sources("new-pattern", ["redis-cache-ttl"], tmp_vault)

        text = note_path.read_text()
        assert "[[new-pattern]]" in text

    def test_backlink_creates_related(self, tmp_vault):
        """## Related section created when missing."""
        from memento_inception import backlink_sources

        note_path = tmp_vault / "notes" / "redis-cache-ttl.md"
        _write_note(
            note_path,
            title="Redis cache requires explicit TTL",
            body="Setting explicit TTL on Redis keys.",
        )

        # Sanity: no Related section yet
        assert "## Related" not in note_path.read_text()

        backlink_sources("new-pattern", ["redis-cache-ttl"], tmp_vault)

        text = note_path.read_text()
        assert "## Related" in text
        assert "- [[new-pattern]]" in text

    def test_backlink_skips_existing(self, tmp_vault):
        """No duplicate link added if already present."""
        from memento_inception import backlink_sources

        note_path = tmp_vault / "notes" / "redis-cache-ttl.md"
        _write_note(
            note_path,
            title="Redis cache requires explicit TTL",
            body="Setting explicit TTL on Redis keys.",
            related=["new-pattern"],
        )

        backlink_sources("new-pattern", ["redis-cache-ttl"], tmp_vault)

        text = note_path.read_text()
        # Count occurrences -- should be exactly 1
        assert text.count("[[new-pattern]]") == 1

    def test_backlink_missing_source(self, tmp_vault):
        """No error when source note does not exist."""
        from memento_inception import backlink_sources

        # notes dir exists but note does not
        backlink_sources("new-pattern", ["nonexistent-note"], tmp_vault)
        # No assertion needed -- just no exception

    def test_backlink_multiple_sources(self, tmp_vault):
        """Backlinks added to all source notes."""
        from memento_inception import backlink_sources

        stems = ["note-a", "note-b", "note-c"]
        for stem in stems:
            _write_note(
                tmp_vault / "notes" / f"{stem}.md",
                title=f"Title for {stem}",
                body=f"Body for {stem}.",
            )

        backlink_sources("cross-pattern", stems, tmp_vault)

        for stem in stems:
            text = (tmp_vault / "notes" / f"{stem}.md").read_text()
            assert "[[cross-pattern]]" in text
