from unittest.mock import patch

from memento.mcp_server import memento_daily_snapshot
from memento.store import write_daily_snapshot


def test_write_daily_snapshot_creates_deterministic_note(tmp_path):
    result = write_daily_snapshot(tmp_path, "2026-04-27", "memento-vault", "Daily notes")

    assert result == {"path": "notes/daily-2026-04-27-memento-vault.md", "supersedes": None, "version": 1}
    note = tmp_path / result["path"]
    assert note.exists()
    content = note.read_text()
    assert "title: Daily 2026-04-27 memento-vault" in content
    assert "type: daily" in content
    assert "repo_slug: memento-vault" in content
    assert "Daily notes" in content


def test_write_daily_snapshot_requires_supersede_for_existing_note(tmp_path):
    first = write_daily_snapshot(tmp_path, "2026-04-27", "memento-vault", "Daily notes")
    second = write_daily_snapshot(tmp_path, "2026-04-27", "memento-vault", "Updated")
    third = write_daily_snapshot(tmp_path, "2026-04-27", "memento-vault", "Updated", supersede=True)

    assert first["version"] == 1
    assert second["reason"] == "already_exists"
    assert third == {
        "path": "notes/daily-2026-04-27-memento-vault-v2.md",
        "supersedes": "daily-2026-04-27-memento-vault",
        "version": 2,
    }
    assert (tmp_path / third["path"]).exists()


def test_write_daily_snapshot_rejects_invalid_inputs(tmp_path):
    assert write_daily_snapshot(tmp_path, "20260427", "repo", "Body")["reason"] == "invalid_date"
    assert write_daily_snapshot(tmp_path, "2026-04-27", "Repo", "Body")["reason"] == "invalid_repo_slug"
    assert write_daily_snapshot(tmp_path, "2026-04-27", "repo", "  ")["reason"] == "empty_content"


def test_mcp_daily_snapshot_uses_write_lock(tmp_path):
    with (
        patch("memento.mcp_server.get_vault", return_value=tmp_path),
        patch("memento.mcp_server.acquire_vault_write_lock", return_value=True) as acquire,
        patch("memento.mcp_server.release_vault_write_lock") as release,
        patch("memento.mcp_server.log_retrieval"),
    ):
        result = memento_daily_snapshot("2026-04-27", "memento-vault", "Daily notes")

    assert result["path"] == "notes/daily-2026-04-27-memento-vault.md"
    acquire.assert_called_once()
    release.assert_called_once()


def test_mcp_daily_snapshot_reports_lock_timeout(tmp_path):
    with (
        patch("memento.mcp_server.get_vault", return_value=tmp_path),
        patch("memento.mcp_server.acquire_vault_write_lock", return_value=False),
    ):
        result = memento_daily_snapshot("2026-04-27", "memento-vault", "Daily notes")

    assert result["reason"] == "lock_timeout"
