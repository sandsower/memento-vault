"""Tests for note writing and store helpers."""

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from memento.store import (
    acquire_vault_write_lock,
    find_dedup_candidates,
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
