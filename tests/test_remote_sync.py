"""Tests for hooks/memento-remote-sync.py."""

import hashlib
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


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
    path.write_text(f"---\ntitle: {title}\ntype: discovery\ntags: [sync]\ncertainty: 3\n---\n\n{body}\n")


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


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

    def test_direct_sync_skips_locally_tombstoned_note_path(self, tmp_path, capsys):
        from memento.archive import record_tombstone

        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"
        note_path = vault / "notes" / "deleted-note.md"
        _write_note(note_path, title="Deleted Note")
        record_tombstone(vault, "notes/deleted-note.md", content_hash=_file_hash(note_path))

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch.object(mod, "store") as store,
            patch.object(sys, "argv", ["memento-remote-sync.py", str(note_path)]),
        ):
            mod.main()

        store.assert_not_called()
        assert "Skip (tombstoned): notes/deleted-note.md" in capsys.readouterr().out

    def test_direct_sync_store_error_exits_nonzero_and_records_failure(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"
        note_path = vault / "notes" / "new-note.md"
        _write_note(note_path, title="New Note")

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch.object(mod, "get", return_value=None),
            patch.object(mod, "store", return_value={"error": "remote unavailable"}),
            patch.object(sys, "argv", ["memento-remote-sync.py", str(note_path)]),
        ):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()

        assert exc_info.value.code != 0
        assert "Sync failed for 1 note" in capsys.readouterr().err
        entries = list(mod.sync_ledger.iter_entries(vault))
        assert entries[-1]["status"] == "error"
        assert entries[-1]["source"] == "notes/new-note.md"

    def test_direct_sync_remote_fetch_failure_exits_nonzero_without_store(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"
        note_path = vault / "notes" / "new-note.md"
        _write_note(note_path, title="New Note")

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch.object(mod, "get", side_effect=RuntimeError("network down")),
            patch.object(mod, "store") as store,
            patch.object(sys, "argv", ["memento-remote-sync.py", str(note_path)]),
        ):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()

        assert exc_info.value.code != 0
        store.assert_not_called()
        err = capsys.readouterr().err
        assert "remote fetch failed" in err
        assert "Sync failed for 1 note" in err

    def test_missing_resolve_conflicts_value_exits_nonzero(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"
        note_path = vault / "notes" / "new-note.md"
        _write_note(note_path, title="New Note")

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch.object(mod, "store") as store,
            patch.object(sys, "argv", ["memento-remote-sync.py", str(note_path), "--resolve-conflicts"]),
        ):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()

        assert exc_info.value.code != 0
        store.assert_not_called()
        assert "Missing value for --resolve-conflicts" in capsys.readouterr().err

    def test_direct_sync_conflict_can_be_resolved_by_local_overwrite(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"
        note_path = vault / "notes" / "same-title.md"
        _write_note(note_path, title="Same Title", body="Local edited body.")
        remote_content = note_path.read_text().replace("Local edited body.", "Remote old body.")

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch.object(
                mod,
                "get",
                return_value={"path": "notes/same-title.md", "content": remote_content},
            ),
            patch.object(mod, "replace_note", return_value={"path": "notes/same-title.md"}) as replace_note,
            patch.object(mod, "store") as store,
            patch.object(
                sys,
                "argv",
                ["memento-remote-sync.py", "--resolve-conflicts", "local", str(note_path)],
            ),
        ):
            mod.main()

        store.assert_not_called()
        replace_note.assert_called_once()
        assert "Resolved: Same Title -> notes/same-title.md" in capsys.readouterr().out
        entries = list(mod.sync_ledger.iter_entries(vault))
        assert entries[-1]["status"] == "ok"
        assert entries[-1]["remote_path"] == "notes/same-title.md"


class TestCatchUp:
    def test_catch_up_pushes_missing_notes(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note_a = vault / "notes" / "note-a.md"
        note_b = vault / "notes" / "note-b.md"
        _write_note(note_a, title="Note A")
        _write_note(note_b, title="Note B")

        # Remote has note-a but not note-b
        remote_inventory = [
            {"path": "notes/note-a.md", "title": "Note A", "hash": _file_hash(note_a)},
        ]

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "store", return_value={"path": "notes/note-b.md"}) as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        mock_store.assert_called_once()
        assert mock_store.call_args[1]["title"] == "Note B" or mock_store.call_args[0][0] == "Note B"
        output = capsys.readouterr().out
        assert "note-b" in output.lower()

    def test_catch_up_skips_notes_already_on_remote(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note_a = vault / "notes" / "note-a.md"
        _write_note(note_a, title="Note A")

        remote_inventory = [
            {"path": "notes/note-a.md", "title": "Note A", "hash": _file_hash(note_a)},
        ]

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        mock_store.assert_not_called()

    def test_catch_up_skips_locally_tombstoned_note_path(self, tmp_path, capsys):
        from memento.archive import record_tombstone

        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note_a = vault / "notes" / "deleted-note.md"
        _write_note(note_a, title="Deleted Note")
        record_tombstone(vault, "notes/deleted-note.md", content_hash=_file_hash(note_a))

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=[]),
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        mock_store.assert_not_called()
        assert "tombstoned 1" in capsys.readouterr().out

    def test_catch_up_treats_hash_mismatch_as_conflict(self, tmp_path, capsys):
        """Hash mismatches are conflicts, not pushable changes.

        store() is append-only — pushing a hash-mismatched note would create
        a `-2.md` duplicate instead of reconciling. Skip and flag instead.
        """
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note_a = vault / "notes" / "note-a.md"
        _write_note(note_a, title="Note A", body="Updated body.")

        remote_inventory = [
            {"path": "notes/note-a.md", "title": "Note A", "hash": "stale_hash_from_old_version"},
        ]

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        mock_store.assert_not_called()
        output = capsys.readouterr().out
        assert "conflict" in output.lower()
        assert "note-a" in output.lower()

    def test_catch_up_aborts_when_inventory_fetch_fails(self, tmp_path, capsys):
        """A failed list_notes() must not be treated as an empty remote.

        If we treat an error as empty, catch-up would bulk-push the entire
        local vault as duplicates.
        """
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        _write_note(vault / "notes" / "note-a.md", title="Note A")
        _write_note(vault / "notes" / "note-b.md", title="Note B")

        # Simulate inventory fetch failure with None sentinel
        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=None),
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()

        mock_store.assert_not_called()
        assert exc_info.value.code != 0

    def test_catch_up_dry_run_does_not_store(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note_a = vault / "notes" / "note-a.md"
        _write_note(note_a, title="Note A")

        remote_inventory = []  # Remote is empty

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up", "--dry-run"]),
        ):
            mod.main()

        mock_store.assert_not_called()
        output = capsys.readouterr().out
        assert "would push" in output.lower() or "would create" in output.lower()

    def test_catch_up_respects_batch_limit(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        # Create 5 notes, all missing from remote
        for i in range(5):
            _write_note(vault / "notes" / f"note-{i}.md", title=f"Note {i}")

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=[]),
            patch.object(mod, "store", return_value={"path": "notes/x.md"}) as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up", "--batch", "2"]),
        ):
            mod.main()

        assert mock_store.call_count == 2

    def test_catch_up_records_in_ledger(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)

        note_a = vault / "notes" / "note-a.md"
        _write_note(note_a, title="Note A")

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=[]),
            patch.object(mod, "store", return_value={"path": "notes/note-a.md"}),
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        ledger_file = vault / ".sync" / "ledger.jsonl"
        assert ledger_file.exists()
        content = ledger_file.read_text()
        assert "note-a" in content
        assert '"status":"ok"' in content.replace(" ", "").replace(": ", ":")

    def test_catch_up_prints_summary(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        _write_note(vault / "notes" / "new.md", title="New Note")
        _write_note(vault / "notes" / "existing.md", title="Existing Note")

        existing_hash = _file_hash(vault / "notes" / "existing.md")
        remote_inventory = [
            {"path": "notes/existing.md", "title": "Existing Note", "hash": existing_hash},
        ]

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "store", return_value={"path": "notes/new.md"}),
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        output = capsys.readouterr().out
        # Should show some kind of summary with counts
        assert "1" in output  # 1 pushed or 1 skipped

    def test_catch_up_noop_when_not_remote(self, tmp_path):
        mod = _load_remote_sync_module()

        with (
            patch.object(mod, "is_remote", return_value=False),
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            with pytest.raises(SystemExit):
                mod.main()

        mock_store.assert_not_called()

    def test_catch_up_skips_hash_mismatch_when_ledger_explains_it(self, tmp_path, capsys):
        """Hash mismatch with matching ledger content_hash = expected metadata divergence.

        write_note() on the remote reconstructs source/date fields, so raw file
        hashes always differ. When the ledger confirms the semantic content was
        already pushed (content_hash match), skip instead of flagging conflict.
        """
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note = vault / "notes" / "synced-note.md"
        _write_note(note, title="Synced Note", body="Same body.")

        # Remote has the same filename but a different raw hash (metadata divergence)
        remote_inventory = [
            {"path": "notes/synced-note.md", "title": "Synced Note", "hash": "different_due_to_metadata"},
        ]

        # Ledger records a successful push with matching content_hash
        parsed = mod.parse_note(note)
        chash = mod.sync_ledger.content_hash(mod._sync_payload(parsed))
        mod.sync_ledger.record(
            vault,
            "note",
            "notes/synced-note.md",
            status="ok",
            content_hash=chash,
            remote_path="notes/synced-note.md",
        )

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        mock_store.assert_not_called()
        output = capsys.readouterr().out
        assert "conflict (hash mismatch" not in output.lower()
        assert "conflicts 0" in output
        assert "ledger-matched 1" in output

    def test_catch_up_conflicts_hash_mismatch_without_ledger(self, tmp_path, capsys):
        """Hash mismatch with no ledger entry = genuine conflict."""
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note = vault / "notes" / "diverged.md"
        _write_note(note, title="Diverged", body="Local version.")

        remote_inventory = [
            {"path": "notes/diverged.md", "title": "Diverged", "hash": "remote_wrote_different_content"},
        ]

        # No ledger entry — we don't know why hashes differ
        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        mock_store.assert_not_called()
        output = capsys.readouterr().out
        assert "conflict" in output.lower()

    def test_catch_up_conflicts_hash_mismatch_with_stale_ledger(self, tmp_path, capsys):
        """Hash mismatch + ledger content_hash also differs = real content change."""
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note = vault / "notes" / "evolved.md"
        _write_note(note, title="Evolved", body="Version 1.")

        # Record ledger for version 1
        parsed_v1 = mod.parse_note(note)
        chash_v1 = mod.sync_ledger.content_hash(mod._sync_payload(parsed_v1))
        mod.sync_ledger.record(
            vault,
            "note",
            "notes/evolved.md",
            status="ok",
            content_hash=chash_v1,
            remote_path="notes/evolved.md",
        )

        # Local content changed to version 2
        _write_note(note, title="Evolved", body="Version 2 — updated locally.")

        remote_inventory = [
            {"path": "notes/evolved.md", "title": "Evolved", "hash": "remote_has_v1_metadata"},
        ]

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        mock_store.assert_not_called()
        output = capsys.readouterr().out
        assert "conflict" in output.lower()

    def test_catch_up_skips_ledger_matched_notes(self, tmp_path, capsys):
        """Notes pushed under a different remote filename must not be re-pushed.

        store() slugifies the title, which can produce a different filename
        than the local one. The ledger records the mapping, so catch-up should
        use it to avoid duplicates.
        """
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note = vault / "notes" / "local-name.md"
        _write_note(note, title="Local Name")

        # Remote has this note under a slugified name — no filename match
        remote_inventory = [
            {"path": "notes/local-name-2.md", "title": "Local Name", "hash": "different"},
        ]

        # Simulate a prior successful push recorded in the ledger
        parsed = mod.parse_note(note)
        chash = mod.sync_ledger.content_hash(mod._sync_payload(parsed))
        mod.sync_ledger.record(
            vault,
            "note",
            "notes/local-name.md",
            status="ok",
            content_hash=chash,
            remote_path="notes/local-name-2.md",
        )

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        mock_store.assert_not_called()
        output = capsys.readouterr().out
        assert "ledger-matched 1" in output

    def test_catch_up_recovers_missing_ledger_from_same_title_remote_note(self, tmp_path, capsys):
        """A lost local ledger must not cause a duplicate append.

        When the remote note exists under a different filename, catch-up fetches
        the unique title match, verifies semantic content, and seeds a fresh
        ledger entry instead of calling append-only store().
        """
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note = vault / "notes" / "local-name.md"
        _write_note(note, title="Local Name", body="Same body.")
        remote_inventory = [
            {"path": "notes/local-name-2.md", "title": "Local Name", "hash": "remote_metadata_hash"},
        ]

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(
                mod,
                "get",
                return_value={"path": "notes/local-name-2.md", "content": note.read_text()},
            ) as get,
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        get.assert_called_once_with("notes/local-name-2.md")
        mock_store.assert_not_called()
        output = capsys.readouterr().out
        assert "ledger-recovered 1" in output
        state = mod.sync_ledger.fold_state(vault)
        entry = state[("note", "notes/local-name.md")]
        assert entry["status"] == "ok"
        assert entry["remote_path"] == "notes/local-name-2.md"

    def test_catch_up_conflict_can_be_resolved_by_local_overwrite(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note = vault / "notes" / "diverged.md"
        _write_note(note, title="Diverged", body="Local edited body.")
        remote_inventory = [
            {"path": "notes/diverged.md", "title": "Diverged", "hash": "remote_old_hash"},
        ]
        remote_content = note.read_text().replace("Local edited body.", "Remote old body.")

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(
                mod,
                "get",
                return_value={"path": "notes/diverged.md", "content": remote_content},
            ),
            patch.object(mod, "replace_note", return_value={"path": "notes/diverged.md"}) as replace_note,
            patch.object(mod, "store") as mock_store,
            patch.object(
                sys,
                "argv",
                ["memento-remote-sync.py", "--catch-up", "--resolve-conflicts=local"],
            ),
        ):
            mod.main()

        mock_store.assert_not_called()
        replace_note.assert_called_once()
        output = capsys.readouterr().out
        assert "resolved 1" in output
        assert "conflicts 0" in output

    def test_catch_up_resolved_conflicts_respect_batch_limit(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        for name in ["one", "two"]:
            _write_note(vault / "notes" / f"{name}.md", title=name.title(), body="Local edited body.")
        remote_inventory = [
            {"path": "notes/one.md", "title": "One", "hash": "remote_old_hash_1"},
            {"path": "notes/two.md", "title": "Two", "hash": "remote_old_hash_2"},
        ]

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(
                mod,
                "get",
                side_effect=lambda path: {
                    "path": path,
                    "content": (vault / path).read_text().replace("Local edited body.", "Remote old body."),
                },
            ),
            patch.object(mod, "replace_note", return_value={"path": "notes/one.md"}) as replace_note,
            patch.object(mod, "store") as mock_store,
            patch.object(
                sys,
                "argv",
                ["memento-remote-sync.py", "--catch-up", "--batch", "1", "--resolve-conflicts=local"],
            ),
        ):
            mod.main()

        mock_store.assert_not_called()
        replace_note.assert_called_once()
        output = capsys.readouterr().out
        assert "resolved 1" in output
        assert "conflicts 1" in output

    def test_failed_conflict_resolution_attempt_consumes_batch_budget(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        for name in ["one", "two", "three"]:
            body = "Local edited body." if name != "three" else "New body."
            _write_note(vault / "notes" / f"{name}.md", title=name.title(), body=body)
        remote_inventory = [
            {"path": "notes/one.md", "title": "One", "hash": "remote_old_hash_1"},
            {"path": "notes/two.md", "title": "Two", "hash": "remote_old_hash_2"},
        ]

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(
                mod,
                "get",
                side_effect=lambda path: {
                    "path": path,
                    "content": (vault / path).read_text().replace("Local edited body.", "Remote old body."),
                },
            ),
            patch.object(mod, "replace_note", return_value={"error": "replace failed"}) as replace_note,
            patch.object(mod, "store") as mock_store,
            patch.object(
                sys,
                "argv",
                ["memento-remote-sync.py", "--catch-up", "--batch", "1", "--resolve-conflicts=local"],
            ),
        ):
            mod.main()

        replace_note.assert_called_once()
        mock_store.assert_not_called()
        output = capsys.readouterr().out
        assert "resolved 0" in output
        assert "conflicts 2" in output

    def test_catch_up_missing_ledger_checks_duplicate_remote_titles_before_pushing(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note = vault / "notes" / "local-name.md"
        _write_note(note, title="Same Title", body="Same body.")
        remote_inventory = [
            {"path": "notes/same-title.md", "title": "Same Title", "hash": "old_hash"},
            {"path": "notes/same-title-2.md", "title": "Same Title", "hash": "matching_hash"},
        ]
        old_content = note.read_text().replace("Same body.", "Old body.")

        def fake_get(path):
            if path == "notes/same-title.md":
                return {"path": path, "content": old_content}
            return {"path": path, "content": note.read_text()}

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "get", side_effect=fake_get) as get,
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        assert get.call_count == 2
        mock_store.assert_not_called()
        state = mod.sync_ledger.fold_state(vault)
        assert state[("note", "notes/local-name.md")]["remote_path"] == "notes/same-title-2.md"

    def test_catch_up_missing_ledger_checks_duplicate_titles_before_filename_conflict(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note = vault / "notes" / "same-title.md"
        _write_note(note, title="Same Title", body="Same body.")
        remote_inventory = [
            {"path": "notes/same-title.md", "title": "Same Title", "hash": "divergent_hash"},
            {"path": "notes/same-title-2.md", "title": "Same Title", "hash": "matching_hash"},
        ]
        divergent_content = note.read_text().replace("Same body.", "Remote divergent body.")

        def fake_get(path):
            if path == "notes/same-title.md":
                return {"path": path, "content": divergent_content}
            return {"path": path, "content": note.read_text()}

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "get", side_effect=fake_get),
            patch.object(mod, "replace_note") as replace_note,
            patch.object(mod, "store") as mock_store,
            patch.object(
                sys,
                "argv",
                ["memento-remote-sync.py", "--catch-up", "--resolve-conflicts=local"],
            ),
        ):
            mod.main()

        mock_store.assert_not_called()
        replace_note.assert_not_called()
        state = mod.sync_ledger.fold_state(vault)
        assert state[("note", "notes/same-title.md")]["remote_path"] == "notes/same-title-2.md"
        output = capsys.readouterr().out
        assert "ledger-recovered 1" in output
        assert "conflicts 0" in output

    def test_catch_up_missing_ledger_conflicts_duplicate_remote_titles_when_none_match(self, tmp_path, capsys):
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note = vault / "notes" / "local-name.md"
        _write_note(note, title="Same Title", body="Local body.")
        remote_inventory = [
            {"path": "notes/same-title.md", "title": "Same Title", "hash": "old_hash_1"},
            {"path": "notes/same-title-2.md", "title": "Same Title", "hash": "old_hash_2"},
        ]

        def fake_get(path):
            return {"path": path, "content": note.read_text().replace("Local body.", f"Remote body {path}.")}

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "get", side_effect=fake_get),
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        mock_store.assert_not_called()
        output = capsys.readouterr().out
        assert "Conflict" in output
        assert "notes/same-title.md, notes/same-title-2.md" in output

    def test_catch_up_pushes_when_content_changed_since_ledger(self, tmp_path, capsys):
        """If local content changed after the ledger recorded a push, push again."""
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note = vault / "notes" / "evolving-note.md"
        _write_note(note, title="Evolving Note", body="Version 1.")

        # Record a ledger entry for the old version
        parsed_v1 = mod.parse_note(note)
        chash_v1 = mod.sync_ledger.content_hash(mod._sync_payload(parsed_v1))
        mod.sync_ledger.record(
            vault,
            "note",
            "notes/evolving-note.md",
            status="ok",
            content_hash=chash_v1,
            remote_path="notes/evolving-note.md",
        )

        # Now update the local note
        _write_note(note, title="Evolving Note", body="Version 2 — updated.")

        # Remote doesn't have a filename match (was stored under slugified name)
        remote_inventory = []

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "store", return_value={"path": "notes/evolving-note.md"}) as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        mock_store.assert_called_once()

    def test_catch_up_conflict_when_content_changed_and_remote_has_old(self, tmp_path, capsys):
        """Changed content + remote has the old version = conflict, not push."""
        mod = _load_remote_sync_module()
        vault = tmp_path / "vault"

        note = vault / "notes" / "diverged-note.md"
        _write_note(note, title="Diverged Note", body="Version 1.")

        # Record ledger with the old push
        parsed_v1 = mod.parse_note(note)
        chash_v1 = mod.sync_ledger.content_hash(mod._sync_payload(parsed_v1))
        mod.sync_ledger.record(
            vault,
            "note",
            "notes/diverged-note.md",
            status="ok",
            content_hash=chash_v1,
            remote_path="notes/diverged-note-2.md",
        )

        # Now update the local note
        _write_note(note, title="Diverged Note", body="Version 2.")

        # Remote has the note under the slugified name from the ledger
        remote_inventory = [
            {"path": "notes/diverged-note-2.md", "title": "Diverged Note", "hash": "old_hash"},
        ]

        with (
            patch.object(mod, "is_remote", return_value=True),
            patch.object(mod, "get_vault", return_value=vault),
            patch("memento.remote_client.list_notes", return_value=remote_inventory),
            patch.object(mod, "store") as mock_store,
            patch.object(sys, "argv", ["memento-remote-sync.py", "--catch-up"]),
        ):
            mod.main()

        mock_store.assert_not_called()
        output = capsys.readouterr().out
        assert "conflict" in output.lower()
