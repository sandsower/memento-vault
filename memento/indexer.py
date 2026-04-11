"""Background vault indexer — scans for unindexed or stale markdown files.

Designed to be called from a hook or cron, not run as a daemon.
Walks vault dirs (notes/, fleeting/, projects/) and indexes .md files
that are missing from or stale in the search database.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

VAULT_DIRS = ("notes", "fleeting", "projects")


def scan_and_index(vault_path: Path | str, backend) -> dict:
    """Walk vault dirs, find .md files not in search.db or with newer mtime.

    Returns: {"indexed": int, "skipped": int, "removed": int}
    """
    vault_path = Path(vault_path)
    conn = backend._get_conn()

    # Step 1: Query all (path, updated_at) pairs from the notes table
    rows = conn.execute("SELECT path, updated_at FROM notes").fetchall()
    db_index: dict[str, float] = {path: updated_at for path, updated_at in rows}

    # Step 2: Walk vault dirs for .md files
    disk_paths: set[str] = set()
    indexed = 0
    skipped = 0

    for dir_name in VAULT_DIRS:
        search_dir = vault_path / dir_name
        if not search_dir.exists():
            continue
        for md_file in search_dir.rglob("*.md"):
            if md_file.is_symlink():
                continue
            rel_path = str(md_file.relative_to(vault_path))
            disk_paths.add(rel_path)

            # Step 3: Check if file needs indexing
            mtime = md_file.stat().st_mtime
            db_mtime = db_index.get(rel_path)

            if db_mtime is not None and mtime <= db_mtime:
                skipped += 1
                continue

            # Not in DB or file is newer — index it
            if backend.index_note(rel_path):
                indexed += 1
                logger.debug("Indexed: %s", rel_path)
            else:
                logger.warning("Failed to index: %s", rel_path)

    # Step 4: Remove DB entries for files no longer on disk
    removed = 0
    stale_paths = set(db_index.keys()) - disk_paths
    for stale_path in stale_paths:
        conn.execute("DELETE FROM notes WHERE path = ?", (stale_path,))
        # Also clean up vector table if it exists
        try:
            conn.execute("DELETE FROM notes_vec WHERE path = ?", (stale_path,))
        except Exception:
            pass
        removed += 1
        logger.debug("Removed stale: %s", stale_path)

    if stale_paths:
        conn.commit()

    return {"indexed": indexed, "skipped": skipped, "removed": removed}


def index_single(vault_path: Path | str, backend, rel_path: str) -> bool:
    """Index a single file. Returns True on success."""
    vault_path = Path(vault_path)
    full_path = vault_path / rel_path
    if not full_path.exists():
        return False
    return backend.index_note(rel_path)
