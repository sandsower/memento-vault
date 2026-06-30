from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from memento import sync_ledger
from memento.archive import (
    ArchiveConflictError,
    ArchiveError,
    export_archive,
    import_archive,
    latest_active_tombstones,
    record_tombstone,
    tombstones_path,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _note(title: str, body: str = "body") -> str:
    return f"---\ntitle: {title}\ntype: discovery\ntags: [archive]\ncertainty: 3\n---\n\n{body}\n"


def test_export_import_reproduces_portable_vault(tmp_path):
    source = tmp_path / "source"
    _write(source / "notes" / "alpha.md", _note("Alpha", "alpha body"))
    _write(source / "fleeting" / "session.jsonl", '{"event":"capture"}\n')
    _write(source / "projects" / "demo.md", "# Demo\n")
    _write(source / "archive" / "bundle" / "evidence.txt", "evidence")
    _write(source / "vault-identity.json", json.dumps({"vault_id": "vault-source", "created": "2026-01-01T00:00:00Z"}))
    sync_ledger.record(source, "note", "notes/alpha.md", status="ok", content_hash="hash-alpha")
    record_tombstone(
        source, "notes/deleted.md", reason="deleted", content_hash="deleted-hash", ts="2026-01-02T00:00:00Z"
    )

    archive = tmp_path / "portable.zip"
    result = export_archive(source, archive)

    assert result["vault_id"] == "vault-source"
    assert result["file_count"] >= 6
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "payload/notes/alpha.md" in names
        assert "payload/.sync/ledger.jsonl" in names
        assert "payload/.memento/tombstones.jsonl" in names
        assert not any(name.startswith("payload/.search/") for name in names)

    dest = tmp_path / "dest"
    imported = import_archive(archive, dest)

    assert imported["written"] + imported["merged"] >= 6
    assert (dest / "vault-identity.json").read_text() == (source / "vault-identity.json").read_text()
    assert (dest / "notes" / "alpha.md").read_text() == (source / "notes" / "alpha.md").read_text()
    assert (dest / "fleeting" / "session.jsonl").read_text() == '{"event":"capture"}\n'
    assert (dest / "projects" / "demo.md").read_text() == "# Demo\n"
    assert (dest / "archive" / "bundle" / "evidence.txt").read_text() == "evidence"
    assert "notes/alpha.md" in (dest / ".sync" / "ledger.jsonl").read_text()
    assert "notes/deleted.md" in tombstones_path(dest).read_text()


def test_import_reproduces_live_note_even_when_old_tombstone_has_same_path(tmp_path):
    source = tmp_path / "source"
    live_body = _note("Recreated", "live copy")
    _write(source / "notes" / "recreated.md", live_body)
    record_tombstone(
        source,
        "notes/recreated.md",
        reason="deleted",
        content_hash=sync_ledger.content_hash(_note("Recreated", "old deleted copy")),
        ts="2026-01-01T00:00:00Z",
    )
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    imported = import_archive(archive, dest)

    assert imported["written"] >= 1
    assert (dest / "notes" / "recreated.md").read_text() == live_body
    if tombstones_path(dest).exists():
        assert "notes/recreated.md" not in tombstones_path(dest).read_text()


def test_import_creates_empty_content_roots(tmp_path):
    source = tmp_path / "source"
    for root in ("notes", "fleeting", "projects", "archive"):
        (source / root).mkdir(parents=True)
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    import_archive(archive, dest)

    for root in ("notes", "fleeting", "projects", "archive"):
        assert (dest / root).is_dir()


def test_import_refuses_to_overwrite_existing_different_markdown_by_default(tmp_path):
    source = tmp_path / "source"
    _write(source / "notes" / "alpha.md", _note("Alpha", "from archive"))
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    _write(dest / "notes" / "alpha.md", _note("Alpha", "local edit"))

    with pytest.raises(ArchiveConflictError) as exc:
        import_archive(archive, dest)

    assert "notes/alpha.md" in str(exc.value)
    assert "local edit" in (dest / "notes" / "alpha.md").read_text()


def test_local_tombstone_prevents_archive_payload_from_reappearing(tmp_path):
    source = tmp_path / "source"
    _write(source / "notes" / "deleted.md", _note("Deleted", "old copy"))
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    record_tombstone(dest, "notes/deleted.md", reason="deleted", content_hash=None, ts="2026-01-03T00:00:00Z")

    imported = import_archive(archive, dest)

    assert imported["tombstone_skipped"] == 1
    assert not (dest / "notes" / "deleted.md").exists()


def test_existing_tombstone_with_old_hash_conflicts_with_recreated_archive_payload(tmp_path):
    source = tmp_path / "source"
    _write(source / "notes" / "recreated.md", _note("Recreated", "new archive content"))
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    record_tombstone(
        dest,
        "notes/recreated.md",
        content_hash=sync_ledger.content_hash(_note("Recreated", "old deleted content")),
    )

    with pytest.raises(ArchiveConflictError) as exc:
        import_archive(archive, dest)

    assert "notes/recreated.md" in str(exc.value)
    assert not (dest / "notes" / "recreated.md").exists()


def test_existing_tombstone_skip_keeps_deletion_for_recreated_archive_payload(tmp_path):
    source = tmp_path / "source"
    _write(source / "notes" / "recreated.md", _note("Recreated", "new archive content"))
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    record_tombstone(
        dest,
        "notes/recreated.md",
        content_hash=sync_ledger.content_hash(_note("Recreated", "old deleted content")),
    )

    imported = import_archive(archive, dest, conflict="skip")

    assert imported["tombstone_skipped"] == 1
    assert not (dest / "notes" / "recreated.md").exists()


def test_existing_tombstone_overwrite_restores_recreated_archive_payload(tmp_path):
    source = tmp_path / "source"
    archive_body = _note("Recreated", "new archive content")
    _write(source / "notes" / "recreated.md", archive_body)
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    record_tombstone(
        dest,
        "notes/recreated.md",
        content_hash=sync_ledger.content_hash(_note("Recreated", "old deleted content")),
    )

    imported = import_archive(archive, dest, conflict="overwrite")

    assert imported["written"] >= 1
    assert imported["merged"] >= 1
    assert (dest / "notes" / "recreated.md").read_text() == archive_body
    assert "notes/recreated.md" not in latest_active_tombstones(dest)


def test_unhashed_tombstone_overwrite_restores_payload_for_future_sync(tmp_path):
    source = tmp_path / "source"
    archive_body = _note("Restored", "new archive content")
    _write(source / "notes" / "restored.md", archive_body)
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    record_tombstone(dest, "notes/restored.md", content_hash=None, ts="2026-01-03T00:00:00Z")

    imported = import_archive(archive, dest, conflict="overwrite")

    assert imported["written"] >= 1
    assert (dest / "notes" / "restored.md").read_text() == archive_body
    assert "notes/restored.md" not in latest_active_tombstones(dest)


def test_matching_hash_tombstone_overwrite_restores_payload_for_future_sync(tmp_path):
    source = tmp_path / "source"
    archive_body = _note("Restored", "same deleted content")
    _write(source / "notes" / "restored.md", archive_body)
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    record_tombstone(
        dest,
        "notes/restored.md",
        content_hash=sync_ledger.content_hash(archive_body),
        ts="2026-01-03T00:00:00Z",
    )

    imported = import_archive(archive, dest, conflict="overwrite")

    assert imported["written"] >= 1
    assert (dest / "notes" / "restored.md").read_text() == archive_body
    assert "notes/restored.md" not in latest_active_tombstones(dest)


def test_stale_tombstone_does_not_block_identical_recreated_local_note(tmp_path):
    source = tmp_path / "source"
    archive_body = _note("Recreated", "new content")
    _write(source / "notes" / "recreated.md", archive_body)
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    record_tombstone(
        dest,
        "notes/recreated.md",
        content_hash=sync_ledger.content_hash(_note("Recreated", "old deleted content")),
        ts="2026-01-03T00:00:00Z",
    )
    _write(dest / "notes" / "recreated.md", archive_body)

    imported = import_archive(archive, dest)

    assert imported["skipped"] >= 1
    assert (dest / "notes" / "recreated.md").read_text() == archive_body
    assert "notes/recreated.md" not in latest_active_tombstones(dest)


def test_conflict_skip_does_not_merge_tombstone_for_kept_local_file(tmp_path):
    source = tmp_path / "source"
    record_tombstone(source, "notes/kept.md", reason="deleted", content_hash=None, ts="2026-01-03T00:00:00Z")
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    _write(dest / "notes" / "kept.md", _note("Kept", "local content"))

    imported = import_archive(archive, dest, conflict="skip")

    assert imported["tombstoned"] == 0
    assert (dest / "notes" / "kept.md").exists()
    if tombstones_path(dest).exists():
        assert "notes/kept.md" not in tombstones_path(dest).read_text()


def test_imported_tombstone_removes_matching_existing_file(tmp_path):
    source = tmp_path / "source"
    deleted_body = _note("Deleted", "same content")
    deleted_hash = sync_ledger.content_hash(deleted_body)
    record_tombstone(source, "notes/deleted.md", reason="deleted", content_hash=deleted_hash, ts="2026-01-03T00:00:00Z")
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    _write(dest / "notes" / "deleted.md", deleted_body)

    imported = import_archive(archive, dest)

    assert imported["tombstoned"] == 1
    assert not (dest / "notes" / "deleted.md").exists()
    assert "notes/deleted.md" in tombstones_path(dest).read_text()


def test_import_rejects_unsupported_payload_paths(tmp_path):
    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "archive_format": "memento-portable-archive",
                    "archive_version": 1,
                    "content_roots": ["notes", "fleeting", "projects", "archive"],
                    "files": [{"path": ".search/index.sqlite", "size": 4}],
                }
            ),
        )
        zf.writestr("payload/.search/index.sqlite", b"data")

    dest = tmp_path / "dest"
    with pytest.raises(ArchiveError):
        import_archive(archive, dest)

    assert not (dest / ".search" / "index.sqlite").exists()


def test_import_rejects_unsupported_manifest_content_roots(tmp_path):
    archive = tmp_path / "malicious-root.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "archive_format": "memento-portable-archive",
                    "archive_version": 1,
                    "content_roots": ["notes", ".git"],
                    "files": [],
                }
            ),
        )

    with pytest.raises(ArchiveError):
        import_archive(archive, tmp_path / "dest")


def test_import_rejects_unsupported_tombstone_paths_without_deleting_files(tmp_path):
    archive = tmp_path / "malicious-tombstone.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "archive_format": "memento-portable-archive",
                    "archive_version": 1,
                    "content_roots": ["notes", "fleeting", "projects", "archive"],
                    "files": [{"path": ".memento/tombstones.jsonl", "size": 1}],
                }
            ),
        )
        zf.writestr(
            "payload/.memento/tombstones.jsonl",
            json.dumps({"ts": "2026-01-01T00:00:00Z", "path": ".git/config", "reason": "deleted"}) + "\n",
        )

    dest = tmp_path / "dest"
    _write(dest / ".git" / "config", "keep me")
    with pytest.raises(ArchiveError):
        import_archive(archive, dest, conflict="overwrite")

    assert (dest / ".git" / "config").read_text() == "keep me"


def test_export_inside_content_root_does_not_include_archive_itself(tmp_path):
    source = tmp_path / "source"
    _write(source / "notes" / "alpha.md", _note("Alpha"))
    archive = source / "archive" / "portable.zip"

    export_archive(source, archive)

    with zipfile.ZipFile(archive) as zf:
        assert "payload/archive/portable.zip" not in zf.namelist()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_export_skips_symlinked_files_that_point_outside_vault(tmp_path):
    source = tmp_path / "source"
    secret = tmp_path / "secret.md"
    secret.write_text("outside vault", encoding="utf-8")
    (source / "notes").mkdir(parents=True)
    os.symlink(secret, source / "notes" / "leak.md")
    archive = tmp_path / "portable.zip"

    export_archive(source, archive)

    with zipfile.ZipFile(archive) as zf:
        assert "payload/notes/leak.md" not in zf.namelist()


def test_import_uses_unique_temp_file_without_clobbering_local_tmp(tmp_path):
    source = tmp_path / "source"
    _write(source / "notes" / "foo.md", _note("Foo"))
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    _write(dest / "notes" / "foo.md.tmp", "keep tmp")

    import_archive(archive, dest)

    assert (dest / "notes" / "foo.md").read_text() == _note("Foo")
    assert (dest / "notes" / "foo.md.tmp").read_text() == "keep tmp"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_import_rejects_existing_symlink_destination_without_mutating_target(tmp_path):
    source = tmp_path / "source"
    _write(source / "notes" / "foo.md", _note("Foo", "archive content"))
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    _write(dest / "notes" / "bar.md", "keep target")
    os.symlink(dest / "notes" / "bar.md", dest / "notes" / "foo.md")

    with pytest.raises(ArchiveError):
        import_archive(archive, dest, conflict="overwrite")

    assert (dest / "notes" / "bar.md").read_text() == "keep target"
    assert os.path.islink(dest / "notes" / "foo.md")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_import_rejects_tombstone_symlink_destination_without_deleting_target(tmp_path):
    source = tmp_path / "source"
    deleted_body = _note("Foo", "deleted content")
    record_tombstone(
        source,
        "notes/foo.md",
        content_hash=sync_ledger.content_hash(deleted_body),
        ts="2026-01-01T00:00:00Z",
    )
    archive = tmp_path / "portable.zip"
    export_archive(source, archive)

    dest = tmp_path / "dest"
    _write(dest / "notes" / "bar.md", deleted_body)
    os.symlink(dest / "notes" / "bar.md", dest / "notes" / "foo.md")

    with pytest.raises(ArchiveError):
        import_archive(archive, dest, conflict="overwrite")

    assert (dest / "notes" / "bar.md").read_text() == deleted_body
    assert os.path.islink(dest / "notes" / "foo.md")


def _init_vault_repo_with_home(tmp_path: Path, vault: Path) -> Path:
    home = tmp_path / "home"
    config_dir = home / ".config" / "memento-vault"
    config_dir.mkdir(parents=True)
    (config_dir / "memento.yml").write_text(f"vault_path: {vault}\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=vault, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=vault, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=vault, check=True)
    return home


def _run_vault_commit(home: Path) -> None:
    subprocess.run(
        ["./hooks/vault-commit.sh", "delete note"],
        check=True,
        cwd=Path(__file__).parent.parent,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
        capture_output=True,
        text=True,
    )


def test_vault_commit_records_tombstone_for_deleted_tracked_note(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "notes" / "deleted.md", _note("Deleted", "tracked body"))
    home = _init_vault_repo_with_home(tmp_path, vault)
    subprocess.run(["git", "add", "-A"], cwd=vault, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=vault, check=True, capture_output=True, text=True)

    (vault / "notes" / "deleted.md").unlink()
    _run_vault_commit(home)

    records = tombstones_path(vault).read_text(encoding="utf-8").splitlines()
    assert any('"path":"notes/deleted.md"' in line and '"reason":"deleted"' in line for line in records)
    assert not (vault / "notes" / "deleted.md").exists()


def test_vault_commit_records_new_deletion_after_restore_record(tmp_path):
    vault = tmp_path / "vault"
    body = _note("Deleted", "tracked body")
    _write(vault / "notes" / "deleted.md", body)
    home = _init_vault_repo_with_home(tmp_path, vault)
    record_tombstone(
        vault,
        "notes/deleted.md",
        content_hash=sync_ledger.content_hash(body),
        ts="2026-01-01T00:00:00Z",
    )
    with tombstones_path(vault).open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"ts": "2026-01-02T00:00:00Z", "path": "notes/deleted.md", "reason": "restored"}) + "\n"
        )
    subprocess.run(["git", "add", "-A"], cwd=vault, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=vault, check=True, capture_output=True, text=True)

    (vault / "notes" / "deleted.md").unlink()
    _run_vault_commit(home)

    records = [json.loads(line) for line in tombstones_path(vault).read_text(encoding="utf-8").splitlines()]
    path_records = [record for record in records if record.get("path") == "notes/deleted.md"]
    assert path_records[-1]["reason"] == "deleted"
    assert "notes/deleted.md" in latest_active_tombstones(vault)


def test_cli_archive_export_import_round_trip(tmp_path):
    source = tmp_path / "source"
    _write(source / "notes" / "alpha.md", _note("Alpha"))
    archive = tmp_path / "portable.zip"
    dest = tmp_path / "dest"

    subprocess.run(
        ["./bin/memento-vault", "archive", "export", "--vault", str(source), str(archive)],
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        ["./bin/memento-vault", "archive", "import", "--vault", str(dest), str(archive)],
        check=True,
        text=True,
        capture_output=True,
    )

    assert (dest / "notes" / "alpha.md").read_text() == _note("Alpha")
