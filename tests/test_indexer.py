"""Tests for memento.indexer — background vault indexer."""

import os
import time

import pytest

from memento.config import reset_config
from memento.embedded_search import EmbeddedSearchBackend
from memento.search_backend import reset_backend


@pytest.fixture
def vault(tmp_path):
    """Create a vault with sample notes."""
    v = tmp_path / "vault"
    for d in ("notes", "fleeting", "projects"):
        (v / d).mkdir(parents=True)

    (v / "notes" / "alpha.md").write_text(
        "---\ntitle: Alpha note\n---\n\nAlpha content about Redis.\n"
    )
    (v / "notes" / "beta.md").write_text(
        "---\ntitle: Beta note\n---\n\nBeta content about PostgreSQL.\n"
    )
    (v / "fleeting" / "daily.md").write_text(
        "---\ntitle: Daily log\n---\n\nWorked on indexer today.\n"
    )
    return v


@pytest.fixture
def backend(vault):
    """Create an EmbeddedSearchBackend pointed at the test vault."""
    db_path = vault / ".search" / "search.db"
    b = EmbeddedSearchBackend(vault_path=vault, db_path=db_path)
    yield b
    b.close()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    reset_backend()
    reset_config()


class TestScanFindsNewFiles:
    """scan_and_index picks up files not yet in the DB."""

    def test_indexes_all_new_files(self, vault, backend):
        from memento.indexer import scan_and_index

        stats = scan_and_index(vault, backend)
        assert stats["indexed"] == 3
        assert stats["skipped"] == 0
        assert stats["removed"] == 0

    def test_indexed_files_are_searchable(self, vault, backend):
        from memento.indexer import scan_and_index

        scan_and_index(vault, backend)
        results = backend.search("Redis", "memento")
        assert any(r["path"] == "notes/alpha.md" for r in results)


class TestScanSkipsIndexed:
    """Already-indexed files with unchanged mtime are skipped."""

    def test_second_scan_skips_all(self, vault, backend):
        from memento.indexer import scan_and_index

        scan_and_index(vault, backend)
        stats = scan_and_index(vault, backend)
        assert stats["indexed"] == 0
        assert stats["skipped"] == 3
        assert stats["removed"] == 0


class TestScanCatchesUpdatedFiles:
    """Files with newer mtime than DB updated_at get re-indexed."""

    def test_modified_file_reindexed(self, vault, backend):
        from memento.indexer import scan_and_index

        scan_and_index(vault, backend)

        # Modify a file — bump mtime to guarantee it's newer
        note = vault / "notes" / "alpha.md"
        note.write_text(
            "---\ntitle: Alpha note\n---\n\nUpdated content about Memcached.\n"
        )
        future = time.time() + 2
        os.utime(note, (future, future))

        stats = scan_and_index(vault, backend)
        assert stats["indexed"] == 1
        assert stats["skipped"] == 2

        # Verify the new content is searchable
        results = backend.search("Memcached", "memento")
        assert any(r["path"] == "notes/alpha.md" for r in results)


class TestScanRemovesDeletedFiles:
    """DB entries for files no longer on disk get removed."""

    def test_deleted_file_removed_from_db(self, vault, backend):
        from memento.indexer import scan_and_index

        scan_and_index(vault, backend)

        (vault / "notes" / "beta.md").unlink()

        stats = scan_and_index(vault, backend)
        assert stats["removed"] == 1
        assert stats["indexed"] == 0
        assert stats["skipped"] == 2

        # Verify the deleted note is gone from search
        assert backend.get("notes/beta.md") is None


class TestIndexSingle:
    """index_single indexes one file and makes it searchable."""

    def test_index_single_success(self, vault, backend):
        from memento.indexer import index_single

        new_note = vault / "projects" / "gamma.md"
        new_note.write_text(
            "---\ntitle: Gamma project\n---\n\nGamma content about Kubernetes.\n"
        )

        result = index_single(vault, backend, "projects/gamma.md")
        assert result is True

        results = backend.search("Kubernetes", "memento")
        assert any(r["path"] == "projects/gamma.md" for r in results)

    def test_index_single_missing_file(self, vault, backend):
        from memento.indexer import index_single

        result = index_single(vault, backend, "notes/nonexistent.md")
        assert result is False
