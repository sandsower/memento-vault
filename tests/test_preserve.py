"""Tests for artifact bundle preservation."""

import json
from unittest.mock import patch

from memento.mcp_server import memento_preserve


class TestPreserveTool:
    def test_preserve_copies_single_file_and_writes_manifest(self, tmp_vault, tmp_path):
        source = tmp_path / "artifact.txt"
        source.write_text("artifact body")

        with patch("memento.mcp_server.get_vault", return_value=tmp_vault):
            result = memento_preserve(str(source), title="Artifact bundle", link_project_index=False)

        assert result["created"] is True
        assert result["moved"] is False
        assert result["archive_path"].startswith("archive/")
        archive_root = tmp_vault / result["archive_path"]
        assert (archive_root / source.name).read_text() == "artifact body"

        manifest = json.loads((archive_root / ".memento" / "manifest.json").read_text())
        assert manifest["source_path"] == str(source)
        assert manifest["archive_path"] == result["archive_path"]
        assert manifest["move"] is False
        assert manifest["file_count"] == 1
        assert manifest["files"][0]["path"] == source.name
        assert result["manifest_path"] == f"{result['archive_path']}/.memento/manifest.json"
        assert result["index_path"] == f"{result['archive_path']}/.memento/index.md"

    def test_preserve_preserves_directory_tree_structure(self, tmp_vault, tmp_path):
        source = tmp_path / "bundle"
        (source / "nested" / "deeper").mkdir(parents=True)
        (source / "nested" / "deeper" / "file.md").write_text("hello")

        with patch("memento.mcp_server.get_vault", return_value=tmp_vault):
            result = memento_preserve(str(source), title="Bundle", link_project_index=False)

        archive_root = tmp_vault / result["archive_path"]
        assert (archive_root / source.name / "nested" / "deeper" / "file.md").read_text() == "hello"
        manifest = json.loads((archive_root / ".memento" / "manifest.json").read_text())
        assert manifest["source_kind"] == "directory"
        assert manifest["files"][0]["path"] == f"{source.name}/nested/deeper/file.md"

    def test_preserve_slug_collision_uses_unique_archive_path(self, tmp_vault, tmp_path):
        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        first.write_text("one")
        second.write_text("two")

        with patch("memento.mcp_server.get_vault", return_value=tmp_vault):
            first_result = memento_preserve(str(first), title="Same bundle", link_project_index=False)
            second_result = memento_preserve(str(second), title="Same bundle", link_project_index=False)

        assert first_result["archive_path"] != second_result["archive_path"]
        assert second_result["archive_path"].endswith("-2")
        assert (tmp_vault / second_result["archive_path"] / second.name).read_text() == "two"

    def test_preserve_move_removes_source(self, tmp_vault, tmp_path):
        source = tmp_path / "move-me.txt"
        source.write_text("move me")

        with patch("memento.mcp_server.get_vault", return_value=tmp_vault):
            result = memento_preserve(str(source), title="Move bundle", move=True, link_project_index=False)

        assert result["moved"] is True
        assert not source.exists()
        assert (tmp_vault / result["archive_path"] / source.name).read_text() == "move me"

    def test_preserve_links_project_index_when_project_known(self, tmp_vault, tmp_path):
        source = tmp_path / "bundle.txt"
        source.write_text("content")

        with patch("memento.mcp_server.get_vault", return_value=tmp_vault):
            result = memento_preserve(
                str(source),
                title="Project bundle",
                project="api-service",
                description="handoff bundle",
            )

        project_file = tmp_vault / "projects" / "api-service.md"
        assert result["project_index_path"] == "projects/api-service.md"
        assert project_file.exists()
        project_text = project_file.read_text()
        assert "## Preserved bundles" in project_text
        assert result["index_path"] in project_text
        assert "handoff bundle" in project_text

    def test_preserve_warns_on_sensitive_files(self, tmp_vault, tmp_path):
        source = tmp_path / "credentials.env"
        source.write_text("AWS_SECRET_ACCESS_KEY=abc123")

        with patch("memento.mcp_server.get_vault", return_value=tmp_vault):
            result = memento_preserve(str(source), title="Sensitive bundle", link_project_index=False)

        assert result["warnings"]
        assert any("secrets" in warning for warning in result["warnings"])
        manifest = json.loads((tmp_vault / result["archive_path"] / ".memento" / "manifest.json").read_text())
        assert manifest["sensitive_files"] == [source.name]

    def test_preserve_rejects_remote_paths_outside_allowed_roots(self, tmp_vault, tmp_path):
        source = tmp_path / "outside.txt"
        source.write_text("content")

        with (
            patch("memento.mcp_server.get_vault", return_value=tmp_vault),
            patch("memento.mcp_server._active_transport", "streamable-http"),
        ):
            result = memento_preserve(str(source), title="Remote bundle", link_project_index=False)

        assert result["reason"] == "remote_path_rejected"
        assert "remote" in result["error"]
