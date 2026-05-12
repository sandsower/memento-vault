"""Tests for note writing and store helpers."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from memento.store import (
    acquire_vault_write_lock,
    append_fleeting_session,
    find_dedup_candidates,
    log_triage_health,
    release_vault_write_lock,
    update_project_index,
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
        assert "tags: [redis, caching]" in text
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

    def test_write_note_triggers_indexing(self, tmp_vault):
        """When embedded backend is active, index_note is called after write."""
        from memento.embedded_search import EmbeddedSearchBackend

        mock_backend = MagicMock(spec=EmbeddedSearchBackend)

        with patch("memento.search_backend.get_backend", return_value=mock_backend):
            path = write_note(
                tmp_vault,
                title="Indexing test note",
                body="Should trigger index_note.",
                note_type="discovery",
                tags=["test"],
            )

        assert path.exists()
        mock_backend.index_note.assert_called_once()
        call_arg = mock_backend.index_note.call_args[0][0]
        assert call_arg.startswith("notes/")
        assert call_arg.endswith(".md")

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
    def test_update_project_index_creates_if_missing(self, tmp_vault):
        update_project_index(tmp_vault, "api-service", "redis-cache-ttl", "Fixed cache invalidation")

        project_file = tmp_vault / "projects" / "api-service.md"
        assert project_file.exists()
        text = project_file.read_text()
        assert "## Notes" in text
        assert "- [[redis-cache-ttl]]" in text
        assert "## Sessions" in text
        assert "Fixed cache invalidation" in text

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
        assert "Fixed cache invalidation" in text

    def test_update_project_index_skips_duplicate_session_line(self, tmp_vault):
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
                    "## Sessions",
                    "",
                    "- 2026-04-01 `sess-123` Fixed cache invalidation",
                ]
            )
        )

        with patch("memento.store.datetime") as mock_datetime:
            mock_now = mock_datetime.now.return_value
            mock_now.strftime.return_value = "2026-04-01"

            update_project_index(tmp_vault, "api-service", "redis-cache-ttl", "`sess-123` Fixed cache invalidation")

        text = project_file.read_text()
        assert text.count("- 2026-04-01 `sess-123` Fixed cache invalidation") == 1
        assert text.count("- [[redis-cache-ttl]]") == 1

    def test_update_project_index_routes_to_activity_log_when_present(self, tmp_vault):
        """When the hub splits Sessions from Activity log, auto-captures land in Activity log."""
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
                    "- [[existing-note]]",
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

        with patch("memento.store.datetime") as mock_datetime:
            mock_now = mock_datetime.now.return_value
            mock_now.strftime.return_value = "2026-04-15"

            update_project_index(tmp_vault, "api-service", "new-note", "MCP store: New note title")

        text = project_file.read_text()
        # Handwritten Sessions entry untouched
        assert "- 2026-04-01 — handwritten session entry with full context" in text
        # New auto-capture lands inside the Activity log section
        activity_pos = text.index("## Activity log")
        sessions_pos = text.index("## Sessions")
        new_line_pos = text.index("- 2026-04-15 MCP store: New note title")
        assert new_line_pos > activity_pos
        # And not inside Sessions
        assert sessions_pos < activity_pos < new_line_pos

    def test_update_project_index_appends_to_mid_file_activity_log(self, tmp_vault):
        """Activity log insertion stays inside the section, before the next heading."""
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
                    "## Activity log",
                    "",
                    "- 2026-04-01 — earlier auto entry",
                    "",
                    "## Decisions",
                    "",
                    "- Keep this section separate.",
                ]
            )
        )

        with patch("memento.store.datetime") as mock_datetime:
            mock_now = mock_datetime.now.return_value
            mock_now.strftime.return_value = "2026-04-15"

            update_project_index(tmp_vault, "api-service", "new-note", "MCP store: New note title")

        text = project_file.read_text()
        new_line_pos = text.index("- 2026-04-15 MCP store: New note title")
        decisions_pos = text.index("## Decisions")
        assert text.index("## Activity log") < new_line_pos < decisions_pos

    def test_update_project_index_preserves_blank_line_for_empty_activity_log(self, tmp_vault):
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
                    "## Activity log",
                    "",
                ]
            )
        )

        with patch("memento.store.datetime") as mock_datetime:
            mock_now = mock_datetime.now.return_value
            mock_now.strftime.return_value = "2026-04-15"

            update_project_index(tmp_vault, "api-service", "new-note", "MCP store: New note title")

        text = project_file.read_text()
        assert "## Activity log\n\n- 2026-04-15 MCP store: New note title\n" in text

    def test_update_project_index_ignores_heading_text_in_body(self, tmp_vault):
        """Inline heading literals must not trigger Activity log routing."""
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
                    "- This note mentions `## Activity log` but does not define it.",
                    "",
                    "## Sessions",
                    "",
                    "- 2026-04-01 — earlier entry",
                ]
            )
        )

        with patch("memento.store.datetime") as mock_datetime:
            mock_now = mock_datetime.now.return_value
            mock_now.strftime.return_value = "2026-04-15"

            update_project_index(tmp_vault, "api-service", "new-note", "MCP store: New note title")

        text = project_file.read_text()
        sessions_pos = text.index("## Sessions")
        new_line_pos = text.index("- 2026-04-15 MCP store: New note title")
        assert sessions_pos < new_line_pos
        assert text.count("## Activity log") == 1

    def test_update_project_index_falls_back_to_sessions(self, tmp_vault):
        """Hubs without an Activity log section still receive auto-captures in Sessions."""
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
                    "- 2026-04-01 — earlier entry",
                ]
            )
        )

        with patch("memento.store.datetime") as mock_datetime:
            mock_now = mock_datetime.now.return_value
            mock_now.strftime.return_value = "2026-04-15"

            update_project_index(tmp_vault, "api-service", "new-note", "MCP store: New note title")

        text = project_file.read_text()
        assert "- 2026-04-15 MCP store: New note title" in text
        assert "## Activity log" not in text


class TestVaultWriteLock:
    def test_vault_write_lock_serialization(self, tmp_vault):
        lock_path = tmp_vault / ".vault-write.lock"

        assert acquire_vault_write_lock(lock_path=str(lock_path)) is True

        result = {"acquired": None}

        def _contender():
            result["acquired"] = acquire_vault_write_lock(lock_path=str(lock_path), timeout=0.1, poll_interval=0.01)

        thread = threading.Thread(target=_contender)
        thread.start()
        thread.join()

        assert result["acquired"] is False

        release_vault_write_lock(lock_path=str(lock_path))
        assert acquire_vault_write_lock(lock_path=str(lock_path), timeout=0.1, poll_interval=0.01) is True
        release_vault_write_lock(lock_path=str(lock_path))


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
