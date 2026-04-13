"""Tests for hooks/memento-remote-sync.py."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).parent.parent


def _load_remote_sync_module():
    spec = importlib.util.spec_from_file_location(
        "memento_remote_sync_test",
        str(REPO_ROOT / "hooks" / "memento-remote-sync.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_note(path: Path, *, title: str, body: str = "Body.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"title: {title}\n"
        "type: discovery\n"
        "tags: [sync]\n"
        "certainty: 3\n"
        "---\n\n"
        f"{body}\n"
    )


class TestRemoteSyncDryRun:
    def test_dry_run_does_not_store_missing_remote_note(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"
        note_path = vault / "notes" / "new-note.md"
        _write_note(note_path, title="New note")

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch.object(mod, "get", return_value=None),
            patch.object(mod, "store") as store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--dry-run", str(note_path)]),
        ):
            mod.main()

        store.assert_not_called()
        assert "Would create: New note" in capsys.readouterr().out

    def test_dry_run_skips_identical_remote_note(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"
        note_path = vault / "notes" / "same-note.md"
        _write_note(note_path, title="Same note", body="Same body.\n\n## Related")

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch.object(
                mod,
                "get",
                return_value={
                    "path": "notes/same-note.md",
                    "content": note_path.read_text() + "\n## Related\n",
                },
            ),
            patch.object(mod, "store") as store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--dry-run", str(note_path)]),
        ):
            mod.main()

        store.assert_not_called()
        assert "Would skip (remote exists, same content): Same note" in capsys.readouterr().out

    def test_dry_run_reports_conflicting_remote_note(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"
        note_path = vault / "notes" / "same-title.md"
        _write_note(note_path, title="Same title", body="Local body.")

        remote_content = note_path.read_text().replace("Local body.", "Remote body.")
        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch.object(
                mod,
                "get",
                return_value={"path": "notes/same-title.md", "content": remote_content},
            ),
            patch.object(mod, "store") as store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--dry-run", str(note_path)]),
        ):
            mod.main()

        store.assert_not_called()
        assert "Would conflict (remote exists, different content): Same title" in capsys.readouterr().out
