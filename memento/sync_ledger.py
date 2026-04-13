"""Append-only sync ledger for remote vault operations.

Tracks every attempt to sync a note or capture a session to a remote vault,
so later runs can:
  * Skip items that were already synced successfully (idempotency by content hash).
  * Find items whose last attempt failed and retry them.

Layout:
  {vault}/.sync/
    ledger.jsonl        # append-only JSONL; one line per attempt
    spool/              # payload bodies for failed attempts (owned by this module;
                        # the legacy spool at vault/spool/remote-failures/ still
                        # exists for pre-ledger triage fallbacks)

Entry schema (each JSONL line):
    {
        "ts": ISO-8601 UTC,
        "kind": "note" | "capture",
        "source": stable identifier (note path or "session:<id>"),
        "content_hash": sha256 hex of the sanitized payload,
        "status": "ok" | "error",
        "remote_path": optional, set on ok,
        "error": optional, set on error,
        "spool_path": optional, set when the payload is persisted for retry,
        "attempt": 1-indexed attempt counter within this source
    }

The ledger never mutates past entries. "Current state" is derived by folding
the stream: for each (kind, source), the last entry wins.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def content_hash(text: str) -> str:
    """Stable hash of the payload we would send to remote.

    Callers should pass the sanitized text so hash comparisons match across
    runs on the same content, not the raw transcript.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ledger_dir(vault: Path) -> Path:
    return Path(vault) / ".sync"


def ledger_path(vault: Path) -> Path:
    return ledger_dir(vault) / "ledger.jsonl"


def spool_dir(vault: Path) -> Path:
    return ledger_dir(vault) / "spool"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(vault: Path, entry: dict) -> None:
    """Append one entry to the ledger. Creates the directory on first write.

    The O_APPEND semantics of plain file writes keep concurrent appends safe
    on POSIX — multiple hooks may race but each line stays intact.
    """
    ledger_dir(vault).mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, separators=(",", ":"), sort_keys=True)
    with open(ledger_path(vault), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def iter_entries(vault: Path):
    """Yield ledger entries in order. Silently skips malformed lines."""
    path = ledger_path(vault)
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # A partial write from a crashed process; skip rather than abort.
                continue


def fold_state(vault: Path) -> dict[tuple[str, str], dict]:
    """Collapse the ledger to the latest entry per (kind, source)."""
    state: dict[tuple[str, str], dict] = {}
    for entry in iter_entries(vault):
        key = (entry.get("kind", ""), entry.get("source", ""))
        if not key[0] or not key[1]:
            continue
        state[key] = entry
    return state


def last_success_hash(vault: Path, kind: str, source: str) -> str | None:
    """Return the content_hash from the most recent successful attempt, or None.

    Used by syncers to skip work when the payload hasn't changed since the
    last confirmed remote write.
    """
    last: str | None = None
    for entry in iter_entries(vault):
        if (
            entry.get("kind") == kind
            and entry.get("source") == source
            and entry.get("status") == "ok"
        ):
            last = entry.get("content_hash") or last
    return last


def pending_retries(vault: Path) -> list[dict]:
    """Return the subset of current state whose last attempt failed.

    Only items whose freshest ledger entry is status=error need retry — a
    later success supersedes an earlier failure.
    """
    return [e for e in fold_state(vault).values() if e.get("status") == "error"]


def attempt_count(vault: Path, kind: str, source: str) -> int:
    """Count prior attempts for a (kind, source) across the whole ledger."""
    n = 0
    for entry in iter_entries(vault):
        if entry.get("kind") == kind and entry.get("source") == source:
            n += 1
    return n


def spool_payload(vault: Path, kind: str, source: str, payload: str) -> Path:
    """Persist a payload body so a future retry can read it back.

    Returns the absolute path to the spooled file. Filename derives from the
    source identifier plus a timestamp so repeated failures don't clobber
    each other.
    """
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in source)[:80]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = spool_dir(vault) / kind / f"{ts}-{safe}.payload"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(payload, encoding="utf-8")
    return dest


def read_spooled(spool_path: str | os.PathLike) -> str | None:
    """Read a previously spooled payload; returns None if missing."""
    p = Path(spool_path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def record(
    vault: Path,
    kind: str,
    source: str,
    *,
    status: str,
    content_hash: str | None = None,
    remote_path: str | None = None,
    error: str | None = None,
    spool_path: str | None = None,
) -> dict:
    """Build an entry, append it, and return it.

    Convenience wrapper around append() that stamps ts and attempt count.
    """
    entry: dict = {
        "ts": _utcnow_iso(),
        "kind": kind,
        "source": source,
        "status": status,
        "attempt": attempt_count(vault, kind, source) + 1,
    }
    if content_hash is not None:
        entry["content_hash"] = content_hash
    if remote_path is not None:
        entry["remote_path"] = remote_path
    if error is not None:
        # Keep errors bounded so one noisy traceback doesn't bloat the ledger.
        entry["error"] = str(error)[:500]
    if spool_path is not None:
        entry["spool_path"] = str(spool_path)
    append(vault, entry)
    return entry
