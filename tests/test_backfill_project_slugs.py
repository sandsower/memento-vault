"""Tests for scripts/backfill_project_slugs.py (MEM-164)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "backfill_project_slugs",
    str(REPO_ROOT / "scripts" / "backfill_project_slugs.py"),
)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


@pytest.fixture(autouse=True)
def _default_config(monkeypatch):
    """Pin tag_aliases/config so the developer's real config never leaks in."""
    monkeypatch.setattr("memento.store.get_config", lambda: {"tag_aliases": {}})


def _write_note_file(vault: Path, name: str, frontmatter_lines: list[str], body: str = "Body.\n") -> Path:
    notes = vault / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    path = notes / f"{name}.md"
    path.write_text("---\n" + "\n".join(frontmatter_lines) + "\n---\n\n" + body, encoding="utf-8")
    return path


class TestClassifyProjectValue:
    def test_absolute_home_path_maps_to_repo_name_slug(self):
        category, slug = backfill.classify_project_value("/home/vic/Projects/Memento-Vault")
        assert category == "path"
        assert slug == "memento-vault"

    def test_absolute_users_path_maps_to_repo_name_slug(self):
        category, slug = backfill.classify_project_value("/Users/vic/Personal/memento-vault")
        assert category == "path"
        assert slug == "memento-vault"

    def test_worktree_path_collapses_to_main_repo_name(self, tmp_path):
        repo = tmp_path / "my-repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        (repo / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "seed"],
            check=True,
        )
        worktree = tmp_path / "worktrees" / "agent-abc123"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", str(worktree), "-b", "slice-branch"],
            check=True,
        )

        category, slug = backfill.classify_project_value(str(worktree))

        assert category == "path"
        assert slug == "my-repo"

    def test_bare_branch_names_are_left_alone_and_reported(self):
        for branch in ("main", "master", "Develop"):
            category, slug = backfill.classify_project_value(branch)
            assert category == "branch"
            assert slug is None

    def test_already_slug_is_normalized_lowercase_dashes(self):
        category, slug = backfill.classify_project_value("My Project")
        assert category == "slug"
        assert slug == "my-project"

    def test_clean_slug_is_unchanged(self):
        category, slug = backfill.classify_project_value("memento-vault")
        assert category == "unchanged"
        assert slug is None


class TestProcessNote:
    def test_path_project_is_rewritten_and_raw_path_preserved(self):
        text = (
            "---\n"
            "title: Legacy note\n"
            "type: discovery\n"
            'tags: ["sync"]\n'
            "project: /home/vic/Projects/memento-vault\n"
            "date: 2026-01-01T10:00\n"
            "---\n\nBody.\n"
        )

        outcome = backfill.process_note(text)

        assert outcome["changed"] is True
        assert "project: memento-vault\n" in outcome["new_text"]
        assert "project_path: /home/vic/Projects/memento-vault\n" in outcome["new_text"]
        # Everything else round-trips verbatim.
        assert "date: 2026-01-01T10:00" in outcome["new_text"]
        assert outcome["new_text"].endswith("\n---\n\nBody.\n")

    def test_unknown_frontmatter_keys_round_trip(self):
        text = (
            "---\n"
            "title: Custom note\n"
            "custom_key: kept-verbatim\n"
            "nested:\n"
            "  child: value\n"
            "project: /Users/vic/work/api\n"
            "---\n\nBody.\n"
        )

        outcome = backfill.process_note(text)

        assert outcome["changed"] is True
        assert "custom_key: kept-verbatim" in outcome["new_text"]
        assert "nested:\n  child: value" in outcome["new_text"]

    def test_existing_project_path_is_not_duplicated(self):
        text = "---\ntitle: Note\nproject: /Users/vic/work/api\nproject_path: /Users/vic/work/api\n---\n\nBody.\n"

        outcome = backfill.process_note(text)

        assert outcome["changed"] is True
        assert outcome["new_text"].count("project_path:") == 1

    def test_branch_name_project_is_untouched(self):
        text = "---\ntitle: Note\nproject: main\n---\n\nBody.\n"

        outcome = backfill.process_note(text)

        assert outcome["changed"] is False
        assert outcome["category"] == "branch"
        assert outcome["new_text"] == text

    def test_body_dashes_never_fabricate_frontmatter(self):
        text = "Intro.\n\n---\n\nproject: /Users/vic/work/api\n\n---\n\nEnd.\n"

        outcome = backfill.process_note(text)

        assert outcome["changed"] is False
        assert outcome["new_text"] == text

    def test_tags_are_normalized_with_write_time_semantics(self):
        text = '---\ntitle: Note\ntags: ["Foo Bar", "SYNC", "sync"]\n---\n\nBody.\n'

        outcome = backfill.process_note(text)

        assert outcome["changed"] is True
        assert 'tags: ["foo-bar", "sync"]' in outcome["new_text"]

    def test_tags_respect_config_alias_map(self, monkeypatch):
        monkeypatch.setattr("memento.store.get_config", lambda: {"tag_aliases": {"bugs": "bug"}})
        text = '---\ntitle: Note\ntags: ["Bugs", "bug"]\n---\n\nBody.\n'

        outcome = backfill.process_note(text)

        assert outcome["changed"] is True
        assert 'tags: ["bug"]' in outcome["new_text"]


class TestRun:
    def test_dry_run_reports_but_does_not_write(self, tmp_path, capsys):
        vault = tmp_path / "vault"
        note = _write_note_file(
            vault,
            "legacy",
            ["title: Legacy", 'tags: ["Foo Bar"]', "project: /home/vic/Projects/memento-vault"],
        )
        original = note.read_text()

        code = backfill.run(vault, apply=False)

        assert code == 0
        assert note.read_text() == original
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert "/home/vic/Projects/memento-vault" in out
        assert "memento-vault" in out
        assert "notes needing rewrite: 1" in out

    def test_apply_rewrites_notes_atomically(self, tmp_path, capsys):
        vault = tmp_path / "vault"
        path_note = _write_note_file(
            vault,
            "legacy-path",
            ["title: Legacy path", 'tags: ["Foo Bar", "sync"]', "project: /Users/vic/other-machine/api-server"],
        )
        branch_note = _write_note_file(vault, "legacy-branch", ["title: Legacy branch", "project: main"])
        clean_note = _write_note_file(vault, "clean", ["title: Clean", 'tags: ["sync"]', "project: api-server"])

        code = backfill.run(vault, apply=True)

        assert code == 0
        rewritten = path_note.read_text()
        assert "project: api-server\n" in rewritten
        assert "project_path: /Users/vic/other-machine/api-server\n" in rewritten
        assert 'tags: ["foo-bar", "sync"]' in rewritten
        # Bare branch names and already-normalized notes are untouched.
        assert "project: main" in branch_note.read_text()
        assert "project_path" not in branch_note.read_text()
        assert "project: api-server" in clean_note.read_text()
        out = capsys.readouterr().out
        assert "APPLIED" in out
        assert "bare branch names left as-is" in out
        assert "main  (1)" in out

    def test_apply_is_idempotent(self, tmp_path):
        vault = tmp_path / "vault"
        note = _write_note_file(
            vault,
            "legacy",
            ["title: Legacy", 'tags: ["Foo Bar"]', "project: /home/vic/Projects/memento-vault"],
        )

        assert backfill.run(vault, apply=True) == 0
        first_pass = note.read_text()
        assert backfill.run(vault, apply=True) == 0

        assert note.read_text() == first_pass

    def test_reports_distinct_tag_counts_before_and_after(self, tmp_path, capsys):
        vault = tmp_path / "vault"
        _write_note_file(vault, "a", ["title: A", 'tags: ["Foo Bar", "sync"]'])
        _write_note_file(vault, "b", ["title: B", 'tags: ["foo-bar", "SYNC"]'])

        assert backfill.run(vault, apply=False) == 0

        out = capsys.readouterr().out
        assert "distinct tags: 4 before -> 2 after" in out

    def test_missing_notes_dir_errors(self, tmp_path, capsys):
        assert backfill.run(tmp_path / "empty-vault", apply=False) == 1
        assert "notes directory not found" in capsys.readouterr().err
