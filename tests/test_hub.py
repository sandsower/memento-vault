"""Tests for project hub regeneration and the two-tier vault map (MEM-160)."""

import os
import time
from datetime import datetime, timezone

import pytest

import memento.graph as graph
from memento.hub import HUB_SECTION_HEADINGS, regenerate_project_hub, regenerate_stale_hubs, vault_map

FIXED_NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)


def _write_note(
    vault, stem, *, title, note_type="discovery", date="2026-06-01T10:00", project="demo", certainty=3, body=""
):
    text = "\n".join(
        [
            "---",
            f"title: {title}",
            f"type: {note_type}",
            "tags: []",
            "source: session",
            f"certainty: {certainty}",
            f"project: {project}",
            f"date: {date}",
            "---",
            "",
            body,
            "",
        ]
    )
    (vault / "notes" / f"{stem}.md").write_text(text)


@pytest.fixture(autouse=True)
def isolate_graph_cache(tmp_path, monkeypatch):
    """Reset the module-level wikilink-graph cache and point its disk cache at tmp_path.

    memento.graph.load_or_build_graph caches in-process (a mutable module
    global) and on disk (RUNTIME_DIR by default); both must be isolated per
    test so hub tests never read a graph built for another test's vault, and
    never touch the real ~/.cache/memento-vault shared with sibling agent
    worktrees running in parallel.
    """
    monkeypatch.setattr(graph, "_GRAPH_CACHE", [None])
    monkeypatch.setattr(graph, "_GRAPH_CACHE_PATH", str(tmp_path / "wikilink-graph-cache.json"))


class TestRegenerateProjectHub:
    def test_fixed_section_schema_in_order(self, tmp_vault):
        _write_note(tmp_vault, "n1", title="First discovery", date="2026-06-01T10:00")
        _write_note(tmp_vault, "n2", title="A decision", note_type="decision", date="2026-06-28T10:00")

        report = regenerate_project_hub(tmp_vault, "demo", now=FIXED_NOW)

        text = (tmp_vault / "projects" / "demo.md").read_text()
        for heading in HUB_SECTION_HEADINGS:
            assert heading in text
        positions = [text.index(heading) for heading in HUB_SECTION_HEADINGS]
        assert positions == sorted(positions), "sections must render in the fixed schema order"
        assert text.startswith("---\ntitle: demo\nproject: demo\n---\n")
        assert "# demo" in text
        assert "2 notes" in text
        assert "generated 2026-07-06T00:00:00Z" in text
        assert report["note_count"] == 2
        assert report["path"] == "projects/demo.md"

    def test_note_count_and_generated_at_always_present(self, tmp_vault):
        report = regenerate_project_hub(tmp_vault, "empty-project", now=FIXED_NOW)
        text = (tmp_vault / "projects" / "empty-project.md").read_text()
        assert report["note_count"] == 0
        assert "0 notes | generated 2026-07-06T00:00:00Z" in text
        # Fixed sections are always present, even with nothing to show.
        assert "_(no notes yet)_" in text
        assert "_(none in the last 30 days)_" in text
        assert "_(no activity yet)_" in text
        assert "_(nothing omitted)_" in text

    def test_idempotent_double_run_byte_identical(self, tmp_vault):
        _write_note(tmp_vault, "n1", title="First discovery", date="2026-06-01T10:00")
        _write_note(tmp_vault, "n2", title="Second discovery", date="2026-06-05T10:00", body="[[n1]]")

        regenerate_project_hub(tmp_vault, "demo", now=FIXED_NOW)
        first = (tmp_vault / "projects" / "demo.md").read_bytes()

        regenerate_project_hub(tmp_vault, "demo", now=FIXED_NOW)
        second = (tmp_vault / "projects" / "demo.md").read_bytes()

        assert first == second

    def test_regeneration_never_reads_existing_hub_content(self, tmp_vault):
        """Rebuild ignores whatever is already on disk -- never parses it, only overwrites."""
        (tmp_vault / "projects" / "demo.md").write_text("garbage that is not even valid markdown {{{")
        _write_note(tmp_vault, "n1", title="First discovery")

        regenerate_project_hub(tmp_vault, "demo", now=FIXED_NOW)

        text = (tmp_vault / "projects" / "demo.md").read_text()
        assert "garbage" not in text
        assert "## Top notes" in text

    def test_corrupted_legacy_hub_is_overwritten_cleanly(self, tmp_vault):
        """Modeled on the real 316-line corrupted memento-vault.md hub (MEM-160)."""
        corrupted_block = "\n".join(
            [
                "---",
                "title: memento-vault",
                "project: memento-vault",
                "---",
                "",
                "## Notes",
                "",
                "- [[some-note]]",
                "",
                "## Sessions",
                "",
                "- 2026-01-01 sess-a summary text",
                "- 2026-01-02 sess-b summary text truncated mid sente",
                "## Sessions",
                "",
                '- 2026-01-05 sess-c { "tool_result": "stray fragment from agent output" }',
                "## Activity log",
                "",
                "- 2026-02-01 auto capture line",
                "- 2026-02-02 another line",
                "",
            ]
        )
        (tmp_vault / "projects" / "memento-vault.md").write_text(corrupted_block * 5)
        _write_note(tmp_vault, "some-note", title="Some note", project="memento-vault", date="2026-06-01T10:00")

        report = regenerate_project_hub(tmp_vault, "memento-vault", now=FIXED_NOW)

        text = (tmp_vault / "projects" / "memento-vault.md").read_text()
        assert text.count("## Sessions") == 0
        assert text.count("## Activity log") == 0
        assert "stray fragment" not in text
        assert "truncated mid sente" not in text
        assert text.count("## Top notes") == 1
        assert len(text.encode("utf-8")) < len(corrupted_block * 5)
        assert report["note_count"] == 1

    def test_25kb_cap_trims_and_records_overflow(self, tmp_vault):
        for i in range(60):
            _write_note(
                tmp_vault,
                f"note-{i:03d}",
                title=f"Note number {i} with a fairly long descriptive title to pad out the byte count",
                date=f"2026-06-{(i % 28) + 1:02d}T10:00",
            )

        report = regenerate_project_hub(tmp_vault, "demo", config={"hub_max_bytes": 900}, now=FIXED_NOW)

        content_bytes = (tmp_vault / "projects" / "demo.md").read_bytes()
        assert len(content_bytes) <= 900
        assert report["bytes"] <= 900
        assert sum(report["overflow"].values()) > 0
        assert report["trimmed_for_size"]
        # Overflow is never silent: whatever got trimmed shows an explicit count.
        text = content_bytes.decode("utf-8")
        assert "not shown" in text

    def test_default_cap_is_25kb(self, tmp_vault):
        from memento.hub import HUB_MAX_BYTES_DEFAULT

        assert HUB_MAX_BYTES_DEFAULT == 25_000

    def test_recent_decisions_excludes_old_decisions(self, tmp_vault):
        _write_note(tmp_vault, "old-decision", title="Old decision", note_type="decision", date="2026-01-01T10:00")
        _write_note(tmp_vault, "new-decision", title="New decision", note_type="decision", date="2026-07-01T10:00")

        report = regenerate_project_hub(tmp_vault, "demo", now=FIXED_NOW)

        assert "new-decision" in report["recent_decisions"]
        assert "old-decision" not in report["recent_decisions"]

    def test_notes_from_other_projects_excluded(self, tmp_vault):
        _write_note(tmp_vault, "mine", title="Mine", project="demo")
        _write_note(tmp_vault, "theirs", title="Theirs", project="other")

        report = regenerate_project_hub(tmp_vault, "demo", now=FIXED_NOW)

        assert report["note_count"] == 1
        assert "mine" in report["top_notes"]
        assert "theirs" not in report["top_notes"]

    def test_ranks_by_inbound_links_without_networkx(self, tmp_vault, monkeypatch):
        """Degrades to a plain wikilink scan when networkx is unavailable (contract #5)."""
        _write_note(tmp_vault, "popular", title="Popular note", date="2026-06-01T10:00")
        _write_note(tmp_vault, "lonely", title="Lonely note", date="2026-06-02T10:00")
        _write_note(tmp_vault, "linker-a", title="Linker A", date="2026-06-03T10:00", body="[[popular]]")
        _write_note(tmp_vault, "linker-b", title="Linker B", date="2026-06-04T10:00", body="[[popular]]")

        monkeypatch.setattr(graph, "_HAS_NETWORKX", False)

        report = regenerate_project_hub(tmp_vault, "demo", now=FIXED_NOW)

        # "popular" has 2 inbound links vs 0 for "lonely" -- must rank first
        # even with no networkx/pagerank available.
        ranked = report["top_notes"]
        assert ranked.index("popular") < ranked.index("lonely")

    def test_writes_via_atomic_replace(self, tmp_vault, monkeypatch):
        replace_calls = []
        real_replace = os.replace

        def recording_replace(src, dst):
            replace_calls.append((src, dst))
            return real_replace(src, dst)

        monkeypatch.setattr("memento.store.os.replace", recording_replace)

        regenerate_project_hub(tmp_vault, "demo", now=FIXED_NOW)

        target = tmp_vault / "projects" / "demo.md"
        writes = [(src, dst) for src, dst in replace_calls if dst == target]
        assert writes, "regenerate_project_hub must write via tmp + os.replace"
        assert list(target.parent.glob(".tmp-*")) == []


class TestVaultMap:
    def test_combines_hub_and_cross_project_notes(self, tmp_vault):
        _write_note(tmp_vault, "mine", title="Mine", project="demo", date="2026-06-01T10:00")
        _write_note(tmp_vault, "theirs", title="Theirs", project="other", date="2026-06-02T10:00")

        text = vault_map(tmp_vault, "demo", config={})

        assert "## Top notes" in text  # from the regenerated hub
        assert "## Cross-project top notes" in text
        assert "[[theirs]]" in text
        assert "[[mine]]" in text.split("## Cross-project top notes")[0]  # mine belongs to tier 1 only

    def test_regenerates_hub_before_assembling(self, tmp_vault):
        """vault_map must never read stale/corrupted disk content -- it regenerates first."""
        (tmp_vault / "projects" / "demo.md").write_text("stale garbage")
        _write_note(tmp_vault, "mine", title="Mine", project="demo")

        text = vault_map(tmp_vault, "demo", config={})

        assert "stale garbage" not in text
        assert "[[mine]]" in text

    def test_capped_at_vault_map_max_bytes(self, tmp_vault):
        _write_note(tmp_vault, "mine", title="Mine", project="demo", date="2026-06-01T10:00")
        for i in range(30):
            _write_note(
                tmp_vault,
                f"cross-{i:03d}",
                title=f"Cross project note {i} with a long descriptive title for padding",
                project=f"other-{i}",
                date=f"2026-06-{(i % 28) + 1:02d}T10:00",
            )

        text = vault_map(tmp_vault, "demo", config={"vault_map_max_bytes": 1200})

        assert len(text.encode("utf-8")) <= 1200

    def test_default_cap_is_25kb(self):
        from memento.hub import VAULT_MAP_MAX_BYTES_DEFAULT

        assert VAULT_MAP_MAX_BYTES_DEFAULT == 25_000

    def test_no_cross_project_notes_renders_placeholder(self, tmp_vault):
        _write_note(tmp_vault, "mine", title="Mine", project="demo")

        text = vault_map(tmp_vault, "demo", config={})

        assert "## Cross-project top notes" in text
        assert "_(none)_" in text


class TestRegenerateStaleHubs:
    def test_disabled_by_default_is_a_noop(self, tmp_vault):
        _write_note(tmp_vault, "n1", title="First", project="demo")

        report = regenerate_stale_hubs(tmp_vault, config={})

        assert report == {"enabled": False, "regenerated": [], "skipped": []}
        assert not (tmp_vault / "projects" / "demo.md").exists()

    def test_regenerates_projects_with_notes_newer_than_hub(self, tmp_vault):
        _write_note(tmp_vault, "n1", title="First", project="demo")

        report = regenerate_stale_hubs(tmp_vault, config={"hub_regeneration_enabled": True})

        assert report["enabled"] is True
        assert report["regenerated"] == ["demo"]
        assert (tmp_vault / "projects" / "demo.md").exists()

    def test_skips_projects_already_current(self, tmp_vault):
        _write_note(tmp_vault, "n1", title="First", project="demo")
        regenerate_stale_hubs(tmp_vault, config={"hub_regeneration_enabled": True})

        # Touch nothing -- a second run should find the hub already current.
        report = regenerate_stale_hubs(tmp_vault, config={"hub_regeneration_enabled": True})

        assert report["regenerated"] == []
        assert report["skipped"] == ["demo"]

    def test_regenerates_again_when_a_note_is_touched_after_the_hub(self, tmp_vault):
        _write_note(tmp_vault, "n1", title="First", project="demo")
        regenerate_stale_hubs(tmp_vault, config={"hub_regeneration_enabled": True})

        # Make the note's mtime strictly newer than the hub file's mtime.
        time.sleep(0.05)
        (tmp_vault / "notes" / "n1.md").write_text((tmp_vault / "notes" / "n1.md").read_text())

        report = regenerate_stale_hubs(tmp_vault, config={"hub_regeneration_enabled": True})

        assert report["regenerated"] == ["demo"]
