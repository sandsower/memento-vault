"""Tests for note writing and store helpers."""

import threading
from pathlib import Path

from memento.store import (
    acquire_vault_write_lock,
    find_dedup_candidates,
    release_vault_write_lock,
    update_project_index,
    write_note,
)


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
