from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from memento import sync_ledger
from memento.archive import (
    ArchiveConflictError,
    export_archive,
    import_archive,
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
    assert (dest / "notes" / "recreated.md").read_text() == archive_body


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
