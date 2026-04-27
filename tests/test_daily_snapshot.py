"""Tests for memento_daily_snapshot MCP tool and write_daily_snapshot helper."""

from pathlib import Path
from unittest.mock import patch

import pytest

from memento.config import DEFAULT_CONFIG
from memento.mcp_server import memento_daily_snapshot
from memento.store import write_daily_snapshot


@pytest.fixture
def vault_config(tmp_vault):
    config = dict(DEFAULT_CONFIG)
    config["vault_path"] = str(tmp_vault)
    return config


@pytest.fixture
def _use_vault_config(vault_config, monkeypatch):
    monkeypatch.setattr("memento.mcp_server.get_config", lambda: vault_config)
    monkeypatch.setattr("memento.mcp_server.get_vault", lambda: Path(vault_config["vault_path"]))
    monkeypatch.setattr("memento.store.get_config", lambda: vault_config)
    monkeypatch.setattr("memento.config._CONFIG", vault_config)


# --- write_daily_snapshot (helper) ---


class TestWriteDailySnapshotValidation:
    def test_invalid_date_format(self, tmp_vault):
        result = write_daily_snapshot(tmp_vault, "04-23-2026", "care_git", "body")
        assert result["reason"] == "invalid_date"

    def test_invalid_date_type(self, tmp_vault):
        result = write_daily_snapshot(tmp_vault, None, "care_git", "body")
        assert result["reason"] == "invalid_date"

    def test_invalid_repo_slug_uppercase(self, tmp_vault):
        result = write_daily_snapshot(tmp_vault, "2026-04-23", "Care_Git", "body")
        assert result["reason"] == "invalid_repo_slug"

    def test_invalid_repo_slug_special_chars(self, tmp_vault):
        result = write_daily_snapshot(tmp_vault, "2026-04-23", "care/git", "body")
        assert result["reason"] == "invalid_repo_slug"

    def test_empty_content(self, tmp_vault):
        result = write_daily_snapshot(tmp_vault, "2026-04-23", "care_git", "   ")
        assert result["reason"] == "empty_content"


class TestWriteDailySnapshotWrite:
    def test_first_write_creates_base_file(self, tmp_vault):
        result = write_daily_snapshot(
            tmp_vault,
            "2026-04-23",
            "care_git",
            "Today we fixed the flaky test.",
        )

        assert result["path"] == "notes/daily-2026-04-23-care_git.md"
        assert result["version"] == 1
        assert result["supersedes"] is None

        target = tmp_vault / "notes" / "daily-2026-04-23-care_git.md"
        assert target.exists()
        content = target.read_text()
        assert "title: Daily 2026-04-23 care_git" in content
        assert "type: daily" in content
        assert "tags: [daily, care_git]" in content
        assert "source: orra" in content
        assert "repo_slug: care_git" in content
        assert "Today we fixed the flaky test." in content
        assert "## Related" in content
        assert "supersedes:" not in content

    def test_already_exists_without_supersede(self, tmp_vault):
        write_daily_snapshot(tmp_vault, "2026-04-23", "care_git", "first")
        result = write_daily_snapshot(tmp_vault, "2026-04-23", "care_git", "second")

        assert result["reason"] == "already_exists"
        assert result["existing_path"] == "notes/daily-2026-04-23-care_git.md"
        # Body of base file should remain "first"
        base = tmp_vault / "notes" / "daily-2026-04-23-care_git.md"
        assert "first" in base.read_text()
        assert "second" not in base.read_text()

    def test_supersede_writes_v2(self, tmp_vault):
        write_daily_snapshot(tmp_vault, "2026-04-23", "care_git", "first")
        result = write_daily_snapshot(
            tmp_vault, "2026-04-23", "care_git", "updated", supersede=True
        )

        assert result["path"] == "notes/daily-2026-04-23-care_git-v2.md"
        assert result["version"] == 2
        assert result["supersedes"] == "daily-2026-04-23-care_git"

        v2 = tmp_vault / "notes" / "daily-2026-04-23-care_git-v2.md"
        assert v2.exists()
        content = v2.read_text()
        assert 'supersedes: "[[daily-2026-04-23-care_git]]"' in content
        assert "updated" in content

    def test_third_write_supersedes_to_v3(self, tmp_vault):
        write_daily_snapshot(tmp_vault, "2026-04-23", "care_git", "first")
        write_daily_snapshot(tmp_vault, "2026-04-23", "care_git", "second", supersede=True)
        result = write_daily_snapshot(
            tmp_vault, "2026-04-23", "care_git", "third", supersede=True
        )

        assert result["path"] == "notes/daily-2026-04-23-care_git-v3.md"
        assert result["version"] == 3
        assert result["supersedes"] == "daily-2026-04-23-care_git"

    def test_different_repos_same_date_independent(self, tmp_vault):
        r1 = write_daily_snapshot(tmp_vault, "2026-04-23", "care_git", "care body")
        r2 = write_daily_snapshot(tmp_vault, "2026-04-23", "fundid", "fundid body")

        assert r1["path"] == "notes/daily-2026-04-23-care_git.md"
        assert r2["path"] == "notes/daily-2026-04-23-fundid.md"
        assert (tmp_vault / "notes" / r1["path"].split("/")[-1]).exists()
        assert (tmp_vault / "notes" / r2["path"].split("/")[-1]).exists()

    def test_sanitizes_secrets(self, tmp_vault):
        body = "Token leaked: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij1234 in logs"
        result = write_daily_snapshot(tmp_vault, "2026-04-23", "care_git", body)

        target = tmp_vault / result["path"]
        content = target.read_text()
        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij1234" not in content
        assert "REDACTED" in content

    def test_frontmatter_extra_merged(self, tmp_vault):
        result = write_daily_snapshot(
            tmp_vault,
            "2026-04-23",
            "care_git",
            "body",
            frontmatter_extra={
                "project": "/home/vic/Projects/care",
                "branch": "main",
                "session_id": "abc123",
            },
        )

        content = (tmp_vault / result["path"]).read_text()
        assert "project: /home/vic/Projects/care" in content
        assert "branch: main" in content
        assert "session_id: abc123" in content

    def test_frontmatter_extra_cannot_clobber_managed(self, tmp_vault):
        result = write_daily_snapshot(
            tmp_vault,
            "2026-04-23",
            "care_git",
            "body",
            frontmatter_extra={
                "title": "Spoofed title",
                "type": "discovery",
                "source": "evil",
                "certainty": 5,
                "tags": ["evil"],
                "supersedes": "[[hacked]]",
                "repo_slug": "other",
                "date": "1999-01-01",
            },
        )

        content = (tmp_vault / result["path"]).read_text()
        lines = content.splitlines()
        title_lines = [ln for ln in lines if ln.startswith("title:")]
        type_lines = [ln for ln in lines if ln.startswith("type:")]
        source_lines = [ln for ln in lines if ln.startswith("source:")]
        certainty_lines = [ln for ln in lines if ln.startswith("certainty:")]
        tags_lines = [ln for ln in lines if ln.startswith("tags:")]
        supersedes_lines = [ln for ln in lines if ln.startswith("supersedes:")]
        repo_slug_lines = [ln for ln in lines if ln.startswith("repo_slug:")]

        assert title_lines == ["title: Daily 2026-04-23 care_git"]
        assert type_lines == ["type: daily"]
        assert source_lines == ["source: orra"]
        assert certainty_lines == ["certainty: 2"]
        assert tags_lines == ["tags: [daily, care_git]"]
        assert supersedes_lines == []  # first write, no supersede chain
        assert repo_slug_lines == ["repo_slug: care_git"]

    def test_frontmatter_injection_via_extras_sanitized(self, tmp_vault):
        result = write_daily_snapshot(
            tmp_vault,
            "2026-04-23",
            "care_git",
            "body",
            frontmatter_extra={"project": "/home/vic\nsource: spoofed"},
        )

        content = (tmp_vault / result["path"]).read_text()
        lines = content.splitlines()
        source_lines = [ln for ln in lines if ln.startswith("source:")]
        assert source_lines == ["source: orra"]


# --- memento_daily_snapshot MCP tool ---


class TestMementoDailySnapshotTool:
    @pytest.mark.usefixtures("_use_vault_config")
    def test_first_write(self, tmp_vault):
        result = memento_daily_snapshot(
            date="2026-04-23",
            repo_slug="care_git",
            content="Today's daily.",
        )

        assert "error" not in result
        assert result["path"] == "notes/daily-2026-04-23-care_git.md"
        assert result["version"] == 1
        assert (tmp_vault / result["path"]).exists()

    @pytest.mark.usefixtures("_use_vault_config")
    def test_invalid_date_returns_error(self, tmp_vault):
        result = memento_daily_snapshot(
            date="not-a-date", repo_slug="care_git", content="body"
        )
        assert result["reason"] == "invalid_date"

    @pytest.mark.usefixtures("_use_vault_config")
    def test_invalid_repo_slug_returns_error(self, tmp_vault):
        result = memento_daily_snapshot(
            date="2026-04-23", repo_slug="Care/Git!", content="body"
        )
        assert result["reason"] == "invalid_repo_slug"

    @pytest.mark.usefixtures("_use_vault_config")
    def test_already_exists(self, tmp_vault):
        memento_daily_snapshot(
            date="2026-04-23", repo_slug="care_git", content="first"
        )
        result = memento_daily_snapshot(
            date="2026-04-23", repo_slug="care_git", content="second"
        )
        assert result["reason"] == "already_exists"

    @pytest.mark.usefixtures("_use_vault_config")
    def test_supersede(self, tmp_vault):
        memento_daily_snapshot(
            date="2026-04-23", repo_slug="care_git", content="first"
        )
        result = memento_daily_snapshot(
            date="2026-04-23",
            repo_slug="care_git",
            content="updated",
            supersede=True,
        )

        assert "error" not in result
        assert result["version"] == 2
        assert result["path"] == "notes/daily-2026-04-23-care_git-v2.md"
        assert result["supersedes"] == "daily-2026-04-23-care_git"

    @pytest.mark.usefixtures("_use_vault_config")
    def test_missing_vault(self, tmp_path):
        missing = tmp_path / "nonexistent"
        with patch("memento.mcp_server.get_vault", return_value=missing):
            result = memento_daily_snapshot(
                date="2026-04-23", repo_slug="care_git", content="body"
            )
        assert result["reason"] == "vault_missing"

    @pytest.mark.usefixtures("_use_vault_config")
    def test_lock_timeout(self, tmp_vault):
        with patch("memento.mcp_server.acquire_vault_write_lock", return_value=False):
            result = memento_daily_snapshot(
                date="2026-04-23", repo_slug="care_git", content="body"
            )
        assert result["reason"] == "lock_timeout"

    @pytest.mark.usefixtures("_use_vault_config")
    def test_frontmatter_extra_passed_through(self, tmp_vault):
        result = memento_daily_snapshot(
            date="2026-04-23",
            repo_slug="care_git",
            content="body",
            frontmatter_extra={
                "project": "/home/vic/Projects/care",
                "branch": "main",
                "session_id": "abc123",
            },
        )

        content = (tmp_vault / result["path"]).read_text()
        assert "project: /home/vic/Projects/care" in content
        assert "branch: main" in content
        assert "session_id: abc123" in content
