"""Tests for note writing and store helpers."""

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memento import store
from memento.store import (
    acquire_vault_write_lock,
    append_fleeting_session,
    apply_access_log_boost,
    durability_tier,
    find_dedup_candidates,
    fold_access_log_into_frontmatter,
    log_triage_health,
    owns_vault_write_lock,
    read_durability_tier,
    record_access,
    release_vault_write_lock,
    replace_note_at_path,
    update_project_index,
    write_daily_snapshot,
    write_note,
)


class TestShimExports:
    """Regression: backwards-compat shim must re-export all store functions."""

    def test_shim_exports_store_functions(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "memento_utils_shim",
            str(Path(__file__).parent.parent / "hooks" / "memento_utils.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        for name in [
            "write_note",
            "update_project_index",
            "append_project_session_line",
            "find_dedup_candidates",
            "acquire_vault_write_lock",
            "release_vault_write_lock",
        ]:
            assert hasattr(mod, name), f"Shim missing export: {name}"


def _write_note_file(directory, stem, title, tags=None):
    path = Path(directory) / f"{stem}.md"
    path.write_text(
        "\n".join(
            [
                "---",
                f"title: {title}",
                "type: discovery",
                f"tags: [{', '.join(tags or [])}]",
                "date: 2026-04-01T12:00",
                "---",
                "",
                "Body.",
            ]
        )
    )
    return path


class TestWriteNote:
    def test_write_note_creates_file_with_frontmatter(self, tmp_vault):
        path = write_note(
            tmp_vault,
            title="Redis cache requires TTL",
            body="Every cache key needs an explicit TTL.",
            note_type="discovery",
            tags=["redis", "caching"],
            certainty=3,
        )

        assert path.exists()
        text = path.read_text()
        assert text.startswith("---\n")
        assert "title: Redis cache requires TTL" in text
        assert "type: discovery" in text
        assert 'tags: ["redis", "caching"]' in text
        assert "source: session" in text
        assert "certainty: 3" in text

    def test_write_note_slugifies_title(self, tmp_vault):
        path = write_note(
            tmp_vault,
            title="Redis / Cache @ TTL",
            body="Body",
            note_type="discovery",
            tags=["redis"],
            certainty=2,
        )

        assert path.name == "redis-cache-ttl.md"

    def test_write_note_auto_fills_defaults(self, tmp_vault):
        path = write_note(
            tmp_vault,
            title="Auto defaults",
            body="Body",
            note_type="decision",
            tags=["notes"],
        )

        text = path.read_text()
        assert "source: session" in text
        assert "date: " in text

    def test_write_note_normalizes_shared_contract_fields(self, tmp_vault):
        path = write_note(
            tmp_vault,
            title="Legacy Pi session",
            body="Body",
            note_type="session",
            tags=["pi", "PI", ""],
            certainty="confirmed",
            source="pi-capture",
            origin="pi_bridge:tool",
            supersedes="[[older-note]]",
        )

        text = path.read_text()
        assert "type: discovery" in text
        assert 'tags: ["pi"]' in text
        assert "source: pi-capture" in text
        assert "origin: pi_bridge:tool" in text
        assert "certainty: 4" in text
        assert 'supersedes: "[[older-note]]"' in text

    def test_write_note_maps_debugging_to_bugfix(self, tmp_vault):
        path = write_note(
            tmp_vault,
            title="Debug note",
            body="Body",
            note_type="debugging",
            tags=["bug"],
        )

        assert "type: bugfix" in path.read_text()

    def test_write_note_serializes_tags_and_supersedes_as_valid_yaml_scalars(self, tmp_vault):
        path = write_note(
            tmp_vault,
            title="Safe frontmatter",
            body="Body",
            note_type="discovery",
            tags=["cache,redis", 'Alice "beta"'],
            supersedes='Alice "beta" note',
        )

        text = path.read_text()
        # Tags are lowercased and space-joined with dashes at write time
        # (MEM-164 vocabulary normalization); supersedes is untouched.
        assert 'tags: ["cache,redis", "alice-\\"beta\\""]' in text
        assert 'supersedes: "Alice \\"beta\\" note"' in text

    def test_write_note_maps_bug_fix_alias_to_bugfix(self, tmp_vault):
        underscore = write_note(
            tmp_vault,
            title="Bug fix underscore",
            body="Body",
            note_type="bug_fix",
            tags=["bug"],
        )
        hyphen = write_note(
            tmp_vault,
            title="Bug fix hyphen",
            body="Body",
            note_type="bug-fix",
            tags=["bug"],
        )

        assert "type: bugfix" in underscore.read_text()
        assert "type: bugfix" in hyphen.read_text()

    def test_write_note_triggers_backend_index_hook(self, tmp_vault):
        """Official writes must notify whichever backend is active, not only embedded search."""

        class FakeBackend:
            def __init__(self):
                self.indexed = []

            def index_note(self, rel_path, collection=None):
                self.indexed.append((rel_path, collection))
                return True

        fake_backend = FakeBackend()

        with patch("memento.search_backend.get_backend", return_value=fake_backend):
            path = write_note(
                tmp_vault,
                title="Indexing test note",
                body="Should trigger index_note.",
                note_type="discovery",
                tags=["test"],
            )

        assert path.exists()
        assert len(fake_backend.indexed) == 1
        call_arg, collection = fake_backend.indexed[0]
        assert collection is None
        assert call_arg.startswith("notes/")
        assert call_arg.endswith(".md")

    def test_write_note_falls_back_to_backend_reindex_when_single_note_hook_absent(self, tmp_vault):
        class ReindexOnlyBackend:
            def __init__(self):
                self.calls = []

            def reindex(self, collection, embed=True):
                self.calls.append((collection, embed))
                return True

        fake_backend = ReindexOnlyBackend()
        with patch("memento.search_backend.get_backend", return_value=fake_backend):
            path = write_note(
                tmp_vault,
                title="Reindex fallback",
                body="Should trigger a cheap backend reindex fallback.",
                note_type="discovery",
                tags=["test"],
            )

        assert path.exists()
        assert fake_backend.calls == [("memento", False)]

    def test_write_note_survives_indexing_failure(self, tmp_vault):
        """If index_note raises, the note is still written successfully."""
        from memento.embedded_search import EmbeddedSearchBackend

        mock_backend = MagicMock(spec=EmbeddedSearchBackend)
        mock_backend.index_note.side_effect = RuntimeError("index exploded")

        with patch("memento.search_backend.get_backend", return_value=mock_backend):
            path = write_note(
                tmp_vault,
                title="Survives index failure",
                body="Note must persist even if indexing blows up.",
                note_type="decision",
                tags=["resilience"],
            )

        assert path.exists()
        assert "Note must persist" in path.read_text()
        mock_backend.index_note.assert_called_once()

    def test_write_note_appends_related_when_body_lacks_one(self, tmp_vault):
        """Bodies without a ``## Related`` section get the canonical placeholder."""
        path = write_note(
            tmp_vault,
            title="No related in body",
            body="Just a plain body.",
            note_type="discovery",
            tags=["test"],
        )

        text = path.read_text()
        assert text.count("## Related") == 1
        assert text.rstrip().endswith("## Related")

    def test_write_note_skips_related_when_body_has_one(self, tmp_vault):
        """Regression: bodies with their own ``## Related`` section must not get a duplicate."""
        body = "Body content with cross-references.\n\n## Related\n- [[note-a]]\n- [[note-b]]\n"
        path = write_note(
            tmp_vault,
            title="Related in body",
            body=body,
            note_type="pattern",
            tags=["test"],
        )

        text = path.read_text()
        assert text.count("## Related") == 1
        assert "[[note-a]]" in text
        assert "[[note-b]]" in text

    def test_write_note_does_not_overwrite_existing(self, tmp_vault):
        """Regression: slug collision must not silently replace an existing note."""
        first = write_note(
            tmp_vault,
            title="Redis cache requires TTL",
            body="Original content.",
            note_type="discovery",
            tags=["redis"],
        )
        second = write_note(
            tmp_vault,
            title="Redis cache requires TTL",
            body="Different content.",
            note_type="discovery",
            tags=["redis"],
        )

        assert first.exists()
        assert second.exists()
        assert first != second
        assert "Original content." in first.read_text()
        assert "Different content." in second.read_text()
        assert second.name == "redis-cache-requires-ttl-2.md"


class TestUniqueTmpNames:
    """Concurrent same-target writers must never share an in-flight tmp file (audit M6)."""

    def _run_interleaved(self, monkeypatch, first_writer, second_writer):
        """Run two writers where the first is paused inside ``os.replace`` until the second finishes.

        This deterministically reproduces the audit M6 race: with slug-derived
        tmp names the second writer clobbers/steals the first writer's tmp file,
        so the first writer's resume crashes or corrupts the note.
        """
        real_replace = os.replace
        first_paused = threading.Event()
        second_done = threading.Event()

        def gated_replace(src, dst):
            if threading.current_thread().name == "writer-first":
                first_paused.set()
                assert second_done.wait(timeout=10), "second writer never finished"
            return real_replace(src, dst)

        monkeypatch.setattr("memento.store.os.replace", gated_replace)

        results = {}
        errors = {}

        def run(name, writer):
            try:
                results[name] = writer()
            except Exception as exc:  # noqa: BLE001 - the assertion below surfaces it
                errors[name] = exc

        first = threading.Thread(target=run, args=("first", first_writer), name="writer-first")
        first.start()
        assert first_paused.wait(timeout=10), "first writer never reached os.replace"
        second = threading.Thread(target=run, args=("second", second_writer), name="writer-second")
        second.start()
        second.join(timeout=10)
        second_done.set()
        first.join(timeout=10)
        assert not first.is_alive() and not second.is_alive()
        return results, errors

    def test_write_note_parallel_same_slug_writers_do_not_collide(self, tmp_vault, monkeypatch):
        results, errors = self._run_interleaved(
            monkeypatch,
            lambda: write_note(
                tmp_vault,
                title="Same slug title",
                body="Body from first writer.",
                note_type="discovery",
                tags=["race"],
            ),
            lambda: write_note(
                tmp_vault,
                title="Same slug title",
                body="Body from second writer.",
                note_type="discovery",
                tags=["race"],
            ),
        )

        assert errors == {}
        notes_dir = tmp_vault / "notes"
        assert list(notes_dir.glob(".tmp-*")) == []
        assert set(results) == {"first", "second"}
        for path in {results["first"], results["second"]}:
            text = path.read_text()
            assert text.startswith("---\n")
            # Each surviving note is one writer's complete render, never a mix.
            assert ("Body from first writer." in text) ^ ("Body from second writer." in text)
            assert text.rstrip().endswith("## Related")

    def test_write_daily_snapshot_parallel_same_target_writers_do_not_collide(self, tmp_vault, monkeypatch):
        results, errors = self._run_interleaved(
            monkeypatch,
            lambda: write_daily_snapshot(tmp_vault, "2026-07-06", "api-service", "First writer content."),
            lambda: write_daily_snapshot(tmp_vault, "2026-07-06", "api-service", "Second writer content."),
        )

        assert errors == {}
        notes_dir = tmp_vault / "notes"
        assert list(notes_dir.glob(".tmp-*")) == []
        for result in results.values():
            assert "error" not in result
        target = notes_dir / "daily-2026-07-06-api-service.md"
        text = target.read_text()
        assert text.startswith("---\n")
        assert ("First writer content." in text) ^ ("Second writer content." in text)

    def test_replace_note_at_path_parallel_same_target_writers_do_not_collide(self, tmp_vault, monkeypatch):
        existing = write_note(
            tmp_vault,
            title="Replace race target",
            body="Original body.",
            note_type="discovery",
            tags=["race"],
        )
        rel_path = f"notes/{existing.name}"

        results, errors = self._run_interleaved(
            monkeypatch,
            lambda: replace_note_at_path(
                tmp_vault,
                rel_path,
                title="Replace race target",
                body="Replacement from first writer.",
                note_type="discovery",
                tags=["race"],
            ),
            lambda: replace_note_at_path(
                tmp_vault,
                rel_path,
                title="Replace race target",
                body="Replacement from second writer.",
                note_type="discovery",
                tags=["race"],
            ),
        )

        assert errors == {}
        notes_dir = tmp_vault / "notes"
        assert list(notes_dir.glob(".tmp-*")) == []
        assert set(results) == {"first", "second"}
        text = existing.read_text()
        assert text.startswith("---\n")
        assert ("Replacement from first writer." in text) ^ ("Replacement from second writer." in text)
        assert "Original body." not in text


class TestReplaceNotePreservesUnknownFrontmatter:
    """Rewrites must round-trip frontmatter keys the write path does not manage (audit M6)."""

    def test_replace_note_at_path_preserves_unknown_frontmatter_keys(self, tmp_vault):
        path = write_note(
            tmp_vault,
            title="Sync target",
            body="Original body.",
            note_type="discovery",
            tags=["sync"],
        )
        text = path.read_text()
        text = text.replace(
            "---\n",
            "---\ncustom-key: keep me\nreview:\n  status: pending\n  owner: vic\n",
            1,
        )
        path.write_text(text)

        target = replace_note_at_path(
            tmp_vault,
            f"notes/{path.name}",
            title="Sync target",
            body="Replaced body.",
            note_type="decision",
            tags=["sync", "updated"],
            certainty=4,
        )

        new_text = target.read_text()
        frontmatter = new_text.split("\n---\n", 1)[0]
        assert "custom-key: keep me" in frontmatter
        assert "review:\n  status: pending\n  owner: vic" in frontmatter
        # Managed keys stay managed (updated, not duplicated).
        assert frontmatter.count("title:") == 1
        assert frontmatter.count("\ntype: ") == 1
        assert "type: decision" in frontmatter
        assert "certainty: 4" in frontmatter
        assert "Replaced body." in new_text
        assert "Original body." not in new_text

    def test_replace_note_at_path_does_not_fabricate_frontmatter_from_body_dashes(self, tmp_vault):
        path = tmp_vault / "notes" / "no-frontmatter.md"
        path.write_text("Intro paragraph.\n\n---\nnot: frontmatter\n---\n\nEnd of body.")

        target = replace_note_at_path(
            tmp_vault,
            "notes/no-frontmatter.md",
            title="No frontmatter",
            body="Clean replacement body.",
            note_type="discovery",
            tags=["sync"],
        )

        new_text = target.read_text()
        assert "not: frontmatter" not in new_text
        assert "Clean replacement body." in new_text


class TestFindDedupCandidates:
    def test_find_dedup_candidates_exact_match(self, tmp_vault):
        _write_note_file(tmp_vault / "notes", "redis-cache-ttl", "Redis cache requires TTL", ["redis", "caching"])

        matches = find_dedup_candidates(tmp_vault, "Redis cache requires TTL", ["redis"])

        assert matches
        assert matches[0].name == "redis-cache-ttl.md"

    def test_find_dedup_candidates_no_match(self, tmp_vault):
        _write_note_file(tmp_vault / "notes", "zustand-reset", "Zustand reset pattern", ["react"])

        matches = find_dedup_candidates(tmp_vault, "Redis cache requires TTL", ["redis"])

        assert matches == []


class TestUpdateProjectIndex:
    """MEM-160: session-summary appends are retired here -- memento.hub.regenerate_project_hub's
    mechanically-derived "## Recent activity" section is the bounded replacement. update_project_index
    now only ever appends a "[[note_name]]" link under "## Notes"; session_summary is accepted for
    call-site compatibility but is otherwise unused."""

    def test_update_project_index_creates_if_missing(self, tmp_vault):
        update_project_index(tmp_vault, "api-service", "redis-cache-ttl", "Fixed cache invalidation")

        project_file = tmp_vault / "projects" / "api-service.md"
        assert project_file.exists()
        text = project_file.read_text()
        assert "## Notes" in text
        assert "- [[redis-cache-ttl]]" in text
        assert "## Sessions" not in text
        assert "Fixed cache invalidation" not in text

    def test_update_project_index_appends_to_existing(self, tmp_vault):
        project_file = tmp_vault / "projects" / "api-service.md"
        project_file.write_text(
            "\n".join(
                [
                    "---",
                    "title: api-service",
                    "project: /home/vic/Projects/api-service",
                    "---",
                    "",
                    "## Notes",
                    "",
                    "- [[existing-note]]",
                    "",
                    "## Sessions",
                    "",
                    "- 2026-04-01 `sess-1` Existing summary",
                ]
            )
        )

        update_project_index(tmp_vault, "api-service", "redis-cache-ttl", "Fixed cache invalidation")

        text = project_file.read_text()
        assert "- [[existing-note]]" in text
        assert "- [[redis-cache-ttl]]" in text
        assert text.count("- [[redis-cache-ttl]]") == 1
        # Legacy Sessions content round-trips untouched -- update_project_index
        # neither adds to it nor removes it.
        assert "- 2026-04-01 `sess-1` Existing summary" in text
        # New session-summary text is never written.
        assert "Fixed cache invalidation" not in text

    def test_update_project_index_is_idempotent_for_repeated_note_links(self, tmp_vault):
        update_project_index(tmp_vault, "api-service", "redis-cache-ttl", "first summary")
        update_project_index(tmp_vault, "api-service", "redis-cache-ttl", "second summary")

        text = (tmp_vault / "projects" / "api-service.md").read_text()
        assert text.count("- [[redis-cache-ttl]]") == 1
        assert "first summary" not in text
        assert "second summary" not in text

    def test_update_project_index_never_writes_session_summary_regardless_of_existing_headings(self, tmp_vault):
        """Retired for every legacy hub shape: Sessions-only, Activity-log-only, or both."""
        project_file = tmp_vault / "projects" / "api-service.md"
        project_file.write_text(
            "\n".join(
                [
                    "---",
                    "title: api-service",
                    "---",
                    "",
                    "## Notes",
                    "",
                    "## Sessions",
                    "",
                    "- 2026-04-01 — handwritten session entry with full context",
                    "",
                    "## Activity log",
                    "",
                    "- 2026-04-01 — [[earlier-auto-capture]]",
                    "",
                ]
            )
        )

        update_project_index(tmp_vault, "api-service", "new-note", "MCP store: New note title")

        text = project_file.read_text()
        # Existing handwritten/legacy content round-trips untouched.
        assert "- 2026-04-01 — handwritten session entry with full context" in text
        assert "- 2026-04-01 — [[earlier-auto-capture]]" in text
        # But no new session-summary text is ever appended anywhere.
        assert "MCP store: New note title" not in text
        assert "- [[new-note]]" in text

    def test_update_project_index_writes_via_atomic_replace(self, tmp_vault, monkeypatch):
        """The project index must be written tmp-then-rename like every other write path (audit M6)."""
        project_file = tmp_vault / "projects" / "api-service.md"
        replace_calls = []
        real_replace = os.replace

        def recording_replace(src, dst):
            replace_calls.append((Path(src), Path(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr("memento.store.os.replace", recording_replace)

        update_project_index(tmp_vault, "api-service", "redis-cache-ttl", "Fixed cache invalidation")

        index_writes = [(src, dst) for src, dst in replace_calls if dst == project_file]
        assert index_writes, "update_project_index must write via tmp + os.replace"
        assert all(src.parent == project_file.parent for src, _ in index_writes)
        assert list(project_file.parent.glob(".tmp-*")) == []
        text = project_file.read_text()
        assert "- [[redis-cache-ttl]]" in text


class TestVaultWriteLock:
    def test_vault_write_lock_serialization(self, tmp_vault):
        lock_file = tmp_vault / ".vault-write.lock"

        assert acquire_vault_write_lock(lock_file=str(lock_file)) is True

        result = {"acquired": None}

        def _contender():
            result["acquired"] = acquire_vault_write_lock(lock_file=str(lock_file), timeout=0.1, poll_interval=0.01)

        thread = threading.Thread(target=_contender)
        thread.start()
        thread.join()

        assert result["acquired"] is False

        release_vault_write_lock(lock_file=str(lock_file))
        assert acquire_vault_write_lock(lock_file=str(lock_file), timeout=0.1, poll_interval=0.01) is True
        release_vault_write_lock(lock_file=str(lock_file))

    def test_lock_path_alias_still_works(self, tmp_vault):
        """The old ``lock_path`` keyword stays accepted for back-compat."""
        lock_file = tmp_vault / ".vault-write.lock"
        assert acquire_vault_write_lock(lock_path=str(lock_file)) is True
        release_vault_write_lock(lock_path=str(lock_file))
        # Acquire-then-release round-trip works through the alias too.
        assert acquire_vault_write_lock(lock_path=str(lock_file), timeout=0.1) is True
        release_vault_write_lock(lock_path=str(lock_file))

    def test_lock_path_and_lock_file_both_set_raises(self, tmp_vault):
        lock_file = tmp_vault / ".vault-write.lock"
        with pytest.raises(TypeError, match="lock_file or lock_path"):
            acquire_vault_write_lock(lock_file=str(lock_file), lock_path=str(lock_file))
        with pytest.raises(TypeError, match="lock_file or lock_path"):
            release_vault_write_lock(lock_file=str(lock_file), lock_path=str(lock_file))


class TestTriageHealthLog:
    def test_log_triage_health_ignores_retrieval_log_config(self, tmp_path):
        health_log = tmp_path / "triage-health.jsonl"

        with (
            patch("memento.store.TRIAGE_HEALTH_LOG_PATH", str(health_log)),
            patch("memento.store.get_config", return_value={"retrieval_log": False}),
        ):
            log_triage_health("structured_notes_llm_failed", session_id="sess-123", project="api-service", error="boom")

        payload = json.loads(health_log.read_text().strip())
        assert payload["hook"] == "triage"
        assert payload["action"] == "structured_notes_llm_failed"
        assert payload["session_id"] == "sess-123"
        assert payload["project"] == "api-service"
        assert payload["error"] == "boom"

    def test_log_triage_health_sanitizes_and_truncates_errors(self, tmp_path):
        health_log = tmp_path / "triage-health.jsonl"
        secret = "sk-" + "a" * 30
        long_error = f"failed with {secret} " + ("x" * 600)

        with patch("memento.store.TRIAGE_HEALTH_LOG_PATH", str(health_log)):
            log_triage_health("structured_notes_llm_failed", session_id="sess-123", error=long_error)

        payload = json.loads(health_log.read_text().strip())
        assert secret not in payload["error"]
        assert "[REDACTED_API_KEY]" in payload["error"]
        assert len(payload["error"]) <= 503


class TestAppendFleetingSession:
    """Vault helper that records a one-line session marker in today's fleeting log."""

    def test_creates_fleeting_file_with_header_and_line(self, tmp_vault):
        """First call for a UTC day creates the file with the date header and entry."""
        moment = datetime(2026, 5, 12, 14, 37, tzinfo=timezone.utc)
        result = append_fleeting_session(
            tmp_vault,
            "ses_abc123",
            cwd="/home/dev/proj",
            branch="main",
            agent="opencode",
            files_edited=["a.py", "b.py", "c.py"],
            now=moment,
        )
        path = tmp_vault / "fleeting" / "2026-05-12.md"
        text = path.read_text()
        assert text.startswith("# 2026-05-12\n\n")
        assert "- 14:37 `ses_abc123` /home/dev/proj (main) — opencode, 3 files\n" in text
        assert result == {"fleeting": "fleeting/2026-05-12.md", "already_logged": False}

    def test_appends_to_existing_file_without_duplicating_header(self, tmp_vault):
        """Subsequent calls on the same UTC day append, leaving the header intact."""
        moment = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
        append_fleeting_session(tmp_vault, "ses_one", agent="claude", now=moment)
        append_fleeting_session(
            tmp_vault,
            "ses_two",
            agent="opencode",
            now=moment.replace(hour=11, minute=15),
        )
        text = (tmp_vault / "fleeting" / "2026-05-12.md").read_text()
        assert text.count("# 2026-05-12\n") == 1
        assert "`ses_one`" in text and "`ses_two`" in text

    def test_dedups_same_session_id(self, tmp_vault):
        """A second call with the same session_id on the same day is a no-op."""
        moment = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
        first = append_fleeting_session(tmp_vault, "ses_dup", agent="opencode", now=moment)
        second = append_fleeting_session(
            tmp_vault,
            "ses_dup",
            agent="opencode",
            now=moment.replace(hour=12),
        )
        assert first["already_logged"] is False
        assert second["already_logged"] is True
        text = (tmp_vault / "fleeting" / "2026-05-12.md").read_text()
        assert text.count("`ses_dup`") == 1

    def test_handles_missing_optional_metadata(self, tmp_vault):
        """Optional fields default sensibly when omitted (cwd → ``?``, others → empty)."""
        moment = datetime(2026, 5, 12, 9, 5, tzinfo=timezone.utc)
        append_fleeting_session(tmp_vault, "ses_bare", now=moment)
        text = (tmp_vault / "fleeting" / "2026-05-12.md").read_text()
        assert "- 09:05 `ses_bare` ? — \n" in text

    def test_distinct_files_per_utc_day(self, tmp_vault):
        """Crossing UTC midnight writes to a new per-day file rather than appending."""
        append_fleeting_session(
            tmp_vault,
            "ses_a",
            agent="claude",
            now=datetime(2026, 5, 11, 23, 59, tzinfo=timezone.utc),
        )
        append_fleeting_session(
            tmp_vault,
            "ses_b",
            agent="claude",
            now=datetime(2026, 5, 12, 0, 1, tzinfo=timezone.utc),
        )
        assert (tmp_vault / "fleeting" / "2026-05-11.md").exists()
        assert (tmp_vault / "fleeting" / "2026-05-12.md").exists()

    def test_normalizes_naive_and_non_utc_datetimes_to_utc(self, tmp_vault):
        """The helper consistently derives the target file and time in UTC."""
        append_fleeting_session(
            tmp_vault,
            "ses_naive",
            now=datetime(2026, 5, 12, 1, 2),
        )
        append_fleeting_session(
            tmp_vault,
            "ses_offset",
            now=datetime(2026, 5, 11, 20, 30, tzinfo=timezone(timedelta(hours=-4))),
        )
        text = (tmp_vault / "fleeting" / "2026-05-12.md").read_text()
        assert "- 01:02 `ses_naive`" in text
        assert "- 00:30 `ses_offset`" in text

    def test_sanitizes_markdown_metadata(self, tmp_vault):
        """Session metadata stays on one safe markdown line."""
        append_fleeting_session(
            tmp_vault,
            "ses`bad\nnext",
            cwd="/tmp/project\nextra",
            branch="main`branch",
            agent="open\ncode",
            now=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
        )
        text = (tmp_vault / "fleeting" / "2026-05-12.md").read_text()
        assert "`ses'bad next` /tmp/project extra (main'branch) — open code" in text


class TestProjectSlugNormalization:
    """MEM-164: write paths store a stable project slug plus the raw path."""

    def test_path_like_project_becomes_slug_with_project_path(self, tmp_vault):
        path = write_note(
            tmp_vault,
            title="Slugged project note",
            body="Body",
            note_type="discovery",
            tags=["sync"],
            project="/home/vic/Projects/Memento-Vault",
        )

        text = path.read_text()
        assert "project: memento-vault\n" in text
        assert "project_path: /home/vic/Projects/Memento-Vault\n" in text

    def test_bare_token_project_is_normalized_not_treated_as_path(self, tmp_vault):
        path = write_note(
            tmp_vault,
            title="Token project note",
            body="Body",
            note_type="discovery",
            tags=["sync"],
            project="My Project",
        )

        text = path.read_text()
        assert "project: my-project\n" in text
        assert "project_path" not in text

    def test_explicit_project_path_round_trips_alongside_slug(self, tmp_vault):
        path = write_note(
            tmp_vault,
            title="Explicit path note",
            body="Body",
            note_type="discovery",
            tags=["sync"],
            project="memento-vault",
            project_path="/Users/vic/Personal/memento-vault",
        )

        text = path.read_text()
        assert "project: memento-vault\n" in text
        assert "project_path: /Users/vic/Personal/memento-vault\n" in text

    def test_replace_note_at_path_preserves_project_path_as_managed_key(self, tmp_vault):
        original = write_note(
            tmp_vault,
            title="Replaceable note",
            body="Original body",
            note_type="discovery",
            tags=["sync"],
            project="/home/vic/Projects/memento-vault",
        )

        target = replace_note_at_path(
            tmp_vault,
            str(original.relative_to(tmp_vault)),
            title="Replaceable note",
            body="Updated body",
            note_type="discovery",
            tags=["sync"],
            project="memento-vault",
            project_path="/home/vic/Projects/memento-vault",
        )

        text = target.read_text()
        assert "project: memento-vault\n" in text
        assert text.count("project_path:") == 1


class TestTagNormalization:
    """MEM-164: tags are normalized at write time; merges come from config tag_aliases."""

    def test_tags_are_lowercased_trimmed_and_dashed(self, tmp_vault):
        path = write_note(
            tmp_vault,
            title="Tagged note",
            body="Body",
            note_type="discovery",
            tags=["  Foo Bar  ", "SYNC", "sync"],
        )

        assert 'tags: ["foo-bar", "sync"]' in path.read_text()

    def test_tag_aliases_from_config_merge_synonyms(self, tmp_vault):
        with patch("memento.store.get_config", return_value={"tag_aliases": {"bugs": "bug", "test": "testing"}}):
            path = write_note(
                tmp_vault,
                title="Aliased tags note",
                body="Body",
                note_type="discovery",
                tags=["Bugs", "bug", "Test"],
            )

        assert 'tags: ["bug", "testing"]' in path.read_text()

    def test_no_stemming_without_explicit_alias(self, tmp_vault):
        with patch("memento.store.get_config", return_value={"tag_aliases": {}}):
            path = write_note(
                tmp_vault,
                title="Plural tags note",
                body="Body",
                note_type="discovery",
                tags=["bugs", "bug"],
            )

        assert 'tags: ["bugs", "bug"]' in path.read_text()


class TestFoldAccessLogIntoFrontmatter:
    """MEM-148: the runtime access log is a write-ahead buffer; frontmatter
    (``resurfaced_count``/``last_resurfaced``) is the durable source of truth.
    """

    @pytest.fixture(autouse=True)
    def _isolated_vault_write_lock(self, monkeypatch, tmp_path):
        """Point the vault write lock at a per-test file (never the real runtime dir)."""
        lock_file = tmp_path / "locks" / "vault-write.lock"
        monkeypatch.setattr("memento.store.VAULT_WRITE_LOCK_PATH", str(lock_file))
        return lock_file

    @pytest.fixture(autouse=True)
    def _patch_config(self, monkeypatch, tmp_vault):
        """Pin both config bindings (memento.config and memento.store) to tmp_vault.

        record_access()/_should_track_access() and _current_vault_id()/
        get_vault_id() resolve get_config() from their own defining module's
        globals, so both bindings need patching for a fully hermetic vault_id
        and access_log_enabled regardless of the real host config.
        """
        cfg = {"vault_path": str(tmp_vault), "access_log_enabled": True}
        monkeypatch.setattr("memento.config.get_config", lambda: cfg, raising=False)
        monkeypatch.setattr("memento.store.get_config", lambda: cfg, raising=False)
        return cfg

    @staticmethod
    def _seed_note(tmp_vault, stem="example", extra_frontmatter=""):
        note_path = tmp_vault / "notes" / f"{stem}.md"
        note_path.write_text(f"---\ntitle: Example\ntype: discovery\ntags: [redis]\n{extra_frontmatter}---\n\nBody.\n")
        return note_path

    def test_fold_writes_count_and_last_resurfaced(self, tmp_vault):
        note_path = self._seed_note(tmp_vault)

        record_access(["notes/example.md"], hook="mcp", tool="search", query="q1", result_count=1)
        record_access(["notes/example.md"], hook="mcp", tool="search", query="q2", result_count=1)

        result = fold_access_log_into_frontmatter(str(tmp_vault))

        assert result == {"folded_notes": 1, "new_events": 2}
        text = note_path.read_text()
        assert "resurfaced_count: 2" in text
        assert "last_resurfaced: " in text
        # Untouched fields and body survive verbatim.
        assert "title: Example" in text
        assert "tags: [redis]" in text
        assert text.endswith("Body.\n")

    def test_fold_survives_a_full_cache_wipe(self, tmp_vault):
        """Acceptance test (MEM-148): a runtime-dir wipe must not reset the signal."""
        self._seed_note(tmp_vault)
        record_access(["notes/example.md"], hook="mcp", tool="search", query="redis ttl", result_count=1)
        record_access(["notes/example.md"], hook="mcp", tool="search", query="redis ttl", result_count=1)

        first = fold_access_log_into_frontmatter(str(tmp_vault))
        assert first["folded_notes"] == 1

        # Simulate a cache cleaner wiping the runtime dir out from under us.
        Path(store.ACCESS_LOG_PATH).unlink(missing_ok=True)
        Path(store.ACCESS_LOG_STATS_PATH).unlink(missing_ok=True)
        store._ACCESS_LOG_CACHE["signature"] = None
        store._ACCESS_LOG_CACHE["stats"] = {}

        baseline = apply_access_log_boost(
            [{"path": "notes/untouched.md", "score": 1.0}],
            config={"access_log_enabled": True, "access_log_boost_weight": 0.2, "vault_path": str(tmp_vault)},
        )
        boosted = apply_access_log_boost(
            [{"path": "notes/example.md", "score": 1.0}],
            config={"access_log_enabled": True, "access_log_boost_weight": 0.2, "vault_path": str(tmp_vault)},
        )

        assert boosted[0]["score"] > baseline[0]["score"]

    def test_fold_is_idempotent(self, tmp_vault):
        note_path = self._seed_note(tmp_vault)
        record_access(["notes/example.md"], hook="mcp", tool="search", query="q1", result_count=1)

        first = fold_access_log_into_frontmatter(str(tmp_vault))
        assert first == {"folded_notes": 1, "new_events": 1}

        second = fold_access_log_into_frontmatter(str(tmp_vault))
        assert second == {"folded_notes": 0, "new_events": 0}

        text = note_path.read_text()
        assert text.count("resurfaced_count:") == 1
        assert "resurfaced_count: 1" in text

    def test_fold_accumulates_across_separate_runs(self, tmp_vault):
        note_path = self._seed_note(tmp_vault)

        record_access(["notes/example.md"], hook="mcp", tool="search", query="q1", result_count=1)
        fold_access_log_into_frontmatter(str(tmp_vault))

        record_access(["notes/example.md"], hook="mcp", tool="search", query="q2", result_count=1)
        second = fold_access_log_into_frontmatter(str(tmp_vault))

        assert second == {"folded_notes": 1, "new_events": 1}
        text = note_path.read_text()
        assert "resurfaced_count: 2" in text

    def test_fold_preserves_unknown_frontmatter_keys(self, tmp_vault):
        note_path = self._seed_note(
            tmp_vault,
            extra_frontmatter='custom_field: keep-me\nsynthesized_from: ["a", "b"]\n',
        )
        record_access(["notes/example.md"], hook="mcp", tool="search", query="q1", result_count=1)

        fold_access_log_into_frontmatter(str(tmp_vault))

        text = note_path.read_text()
        assert "custom_field: keep-me" in text
        assert 'synthesized_from: ["a", "b"]' in text
        assert "resurfaced_count: 1" in text
        assert "last_resurfaced: " in text
        assert "title: Example" in text
        assert text.endswith("Body.\n")

    def test_fold_skips_notes_without_a_frontmatter_block(self, tmp_vault):
        note_path = tmp_vault / "notes" / "no-frontmatter.md"
        note_path.write_text("Just a body, no frontmatter.\n")
        record_access(["notes/no-frontmatter.md"], hook="mcp", tool="search", query="q1", result_count=1)

        result = fold_access_log_into_frontmatter(str(tmp_vault))

        assert result["folded_notes"] == 0
        assert note_path.read_text() == "Just a body, no frontmatter.\n"

    def test_fold_ignores_missing_notes_and_traversal_paths(self, tmp_vault):
        record_access(["notes/does-not-exist.md"], hook="mcp", tool="search", query="q1", result_count=1)
        record_access(["../outside-vault.md"], hook="mcp", tool="search", query="q2", result_count=1)

        result = fold_access_log_into_frontmatter(str(tmp_vault))

        assert result["folded_notes"] == 0

    def test_fold_is_reentrant_with_a_caller_held_lock(self, tmp_vault):
        note_path = self._seed_note(tmp_vault)
        record_access(["notes/example.md"], hook="mcp", tool="search", query="q1", result_count=1)

        assert acquire_vault_write_lock() is True
        try:
            result = fold_access_log_into_frontmatter(str(tmp_vault))
            assert result["folded_notes"] == 1
            # fold() must not release a lock it did not acquire itself.
            assert owns_vault_write_lock() is True
        finally:
            release_vault_write_lock()

        assert "resurfaced_count: 1" in note_path.read_text()


class TestDurabilityTier:
    """MEM-150: derived durability tier decouples decay immunity from certainty.

    Tiers, most to least durable: pinned > hot > warm > cold. Certainty never
    enters this computation -- see TestCoerceCertaintyClamping for the
    (separate) write-time certainty guard.
    """

    NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)

    def test_pinned_wins_regardless_of_resurfacing(self):
        frontmatter = "title: x\npinned: true\n"
        assert durability_tier(frontmatter, now=self.NOW) == "pinned"

    def test_pinned_false_is_not_pinned(self):
        frontmatter = "title: x\npinned: false\n"
        assert durability_tier(frontmatter, now=self.NOW) == "cold"

    def test_hot_within_default_window(self):
        frontmatter = "resurfaced_count: 3\nlast_resurfaced: 2026-06-20T00:00:00Z\n"  # 16 days ago
        assert durability_tier(frontmatter, now=self.NOW) == "hot"

    def test_warm_outside_default_window(self):
        frontmatter = "resurfaced_count: 3\nlast_resurfaced: 2026-01-01T00:00:00Z\n"  # ~186 days ago
        assert durability_tier(frontmatter, now=self.NOW) == "warm"

    def test_cold_never_resurfaced(self):
        frontmatter = "title: x\n"
        assert durability_tier(frontmatter, now=self.NOW) == "cold"

    def test_cold_when_count_is_zero(self):
        frontmatter = "resurfaced_count: 0\n"
        assert durability_tier(frontmatter, now=self.NOW) == "cold"

    def test_pinned_overrides_hot(self):
        frontmatter = "pinned: true\nresurfaced_count: 5\nlast_resurfaced: 2026-06-20T00:00:00Z\n"
        assert durability_tier(frontmatter, now=self.NOW) == "pinned"

    def test_hot_window_is_configurable(self):
        """The same last_resurfaced timestamp reclassifies under a narrower window (MEM-150)."""
        frontmatter = "resurfaced_count: 1\nlast_resurfaced: 2026-06-20T00:00:00Z\n"  # 16 days ago
        assert durability_tier(frontmatter, now=self.NOW, hot_window_days=30) == "hot"
        assert durability_tier(frontmatter, now=self.NOW, hot_window_days=10) == "warm"

    def test_naive_now_is_treated_as_utc(self):
        """Callers that pass a naive `now` (matching apply_temporal_decay's own
        naive datetime.now()) must not crash or silently misclassify."""
        frontmatter = "resurfaced_count: 1\nlast_resurfaced: 2026-06-20T00:00:00Z\n"
        naive_now = datetime(2026, 7, 6, 12, 0)
        assert durability_tier(frontmatter, now=naive_now) == "hot"


class TestReadDurabilityTier:
    """File-reading wrapper around durability_tier(), for search.py/MEM-152 reuse."""

    def test_reads_pinned_frontmatter_from_disk(self, tmp_vault):
        note = tmp_vault / "notes" / "example.md"
        note.write_text("---\ntitle: Example\ntype: discovery\ntags: []\npinned: true\n---\n\nBody.\n")

        assert read_durability_tier(tmp_vault, "notes/example.md") == "pinned"

    def test_missing_note_is_cold(self, tmp_vault):
        assert read_durability_tier(tmp_vault, "notes/missing.md") == "cold"

    def test_path_traversal_is_cold(self, tmp_vault):
        assert read_durability_tier(tmp_vault, "../outside-vault.md") == "cold"

    def test_respects_config_hot_window(self, tmp_vault):
        note = tmp_vault / "notes" / "example.md"
        now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        note.write_text(
            "---\ntitle: Example\ntype: discovery\ntags: []\n"
            "resurfaced_count: 1\nlast_resurfaced: 2026-06-20T00:00:00Z\n---\n\nBody.\n"
        )

        wide = read_durability_tier(tmp_vault, "notes/example.md", config={"durability_hot_window_days": 30}, now=now)
        narrow = read_durability_tier(tmp_vault, "notes/example.md", config={"durability_hot_window_days": 10}, now=now)

        assert wide == "hot"
        assert narrow == "warm"


class TestCoerceCertaintyClamping:
    """MEM-150: out-of-range certainty is clamped at write time, never rejected."""

    def test_write_note_clamps_high_out_of_range_certainty(self, tmp_vault, capsys):
        path = write_note(tmp_vault, title="Bad certainty", body="Body.", note_type="discovery", tags=[], certainty=95)

        assert "certainty: 5" in path.read_text()
        assert "clamped" in capsys.readouterr().err

    def test_write_note_clamps_low_out_of_range_certainty(self, tmp_vault, capsys):
        path = write_note(tmp_vault, title="Zero certainty", body="Body.", note_type="discovery", tags=[], certainty=0)

        assert "certainty: 1" in path.read_text()
        assert "clamped" in capsys.readouterr().err

    def test_write_note_in_range_certainty_is_unwarned(self, tmp_vault, capsys):
        write_note(tmp_vault, title="Fine", body="Body.", note_type="discovery", tags=[], certainty=4)

        assert capsys.readouterr().err == ""

    def test_write_note_still_drops_unparseable_certainty(self, tmp_vault):
        """Unusable (non-numeric, unmapped) input still yields no certainty line at all."""
        path = write_note(
            tmp_vault, title="Bogus", body="Body.", note_type="discovery", tags=[], certainty="not-a-number"
        )

        assert "certainty:" not in path.read_text()
