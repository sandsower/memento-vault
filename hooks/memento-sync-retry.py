#!/usr/bin/env python3
"""Retry remote vault syncs whose last ledger attempt failed.

Usage:
  memento-sync-retry.py            # retry everything pending
  memento-sync-retry.py --list     # just print what would be retried
  memento-sync-retry.py --max N    # cap retries per run

Reads {vault}/.sync/ledger.jsonl, finds entries whose freshest state is
status=error, and replays them:

  * kind=note     — re-read the note file and call remote_client.store()
  * kind=capture  — read the spooled body and call remote_client.capture()

Every replay appends a new ledger entry (ok or error), so the ledger stays
the single source of truth for what's pending.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from memento import sync_ledger  # noqa: E402
from memento.config import get_vault  # noqa: E402
from memento.remote_client import capture, is_remote, store  # noqa: E402


def _parse_note(path: str) -> dict | None:
    """Minimal frontmatter parser — mirrors hooks/memento-remote-sync.py.

    Inlined rather than imported because the sibling script is dash-cased
    ("memento-remote-sync.py") and not a valid Python module name. Keeping
    this parser here avoids runtime importlib juggling and makes the retry
    command self-contained.
    """
    raw = Path(path).read_text()
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return None
    fm, body = parts[1], parts[2].strip()
    if not body:
        return None

    def _pick(name, fm=fm):
        m = re.search(rf"^{name}:\s*(.+)$", fm, re.MULTILINE)
        return m.group(1).strip().strip("\"'") if m else None

    tags_m = re.search(r"^tags:\s*\[(.+)\]", fm, re.MULTILINE)
    tags = (
        [t.strip().strip("\"'") for t in tags_m.group(1).split(",")] if tags_m else []
    )
    certainty_m = re.search(r"^certainty:\s*(\d+)", fm, re.MULTILINE)
    return {
        "title": _pick("title") or Path(path).stem,
        "body": body,
        "note_type": _pick("type") or "discovery",
        "tags": tags,
        "certainty": int(certainty_m.group(1)) if certainty_m else None,
        "project": _pick("project"),
        "branch": _pick("branch"),
        "validity_context": _pick("validity-context"),
    }


def _retry_note(vault: Path, entry: dict) -> dict:
    """Re-read the local note and re-send it."""
    source = entry["source"]
    # source is relative-to-vault when possible; fall back to absolute.
    candidate = vault / source
    note_path = candidate if candidate.exists() else Path(source)

    if not note_path.exists():
        err = f"note file missing: {note_path}"
        return sync_ledger.record(
            vault, "note", source,
            status="error", content_hash=entry.get("content_hash"), error=err,
        )

    note = _parse_note(str(note_path))
    if not note:
        err = f"note unparseable: {note_path}"
        return sync_ledger.record(
            vault, "note", source,
            status="error", content_hash=entry.get("content_hash"), error=err,
        )

    result = store(**note)
    if isinstance(result, dict) and "error" in result:
        return sync_ledger.record(
            vault, "note", source,
            status="error",
            content_hash=entry.get("content_hash"),
            error=result["error"],
            spool_path=entry.get("spool_path"),
        )

    return sync_ledger.record(
        vault, "note", source,
        status="ok",
        content_hash=entry.get("content_hash"),
        remote_path=(result or {}).get("path"),
    )


def _load_capture_envelope(spool_path: str | None) -> tuple[dict | None, str]:
    """Read a spooled capture and return (envelope_dict, source_kind).

    source_kind is one of:
      "envelope"  — JSON envelope written by the current triage (has all args)
      "legacy"    — markdown note with frontmatter written by older triage
      "missing"   — nothing found on disk

    Envelopes give us everything needed for a faithful replay. Legacy spools
    only have the sanitized body, so we signal that to the caller so it can
    warn about degraded metadata instead of silently misclassifying.
    """
    if not spool_path:
        return None, "missing"

    raw = sync_ledger.read_spooled(spool_path)
    if raw is None:
        p = Path(spool_path)
        if not p.exists():
            return None, "missing"
        raw = p.read_text(encoding="utf-8")

    # Try JSON envelope first.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "session_summary" in parsed:
            return parsed, "envelope"
    except json.JSONDecodeError:
        pass

    # Legacy markdown spool: frontmatter + body.
    body = raw
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) >= 3:
            body = parts[2].strip()
    return {"session_summary": body, "_legacy": True}, "legacy"


def _retry_capture(vault: Path, entry: dict) -> dict:
    """Re-send a spooled session capture with its original metadata."""
    source = entry["source"]
    spool_path = entry.get("spool_path")

    envelope, kind = _load_capture_envelope(spool_path)
    if envelope is None:
        err = f"no spooled payload for {source}"
        return sync_ledger.record(
            vault, "capture", source,
            status="error", content_hash=entry.get("content_hash"), error=err,
        )

    # Resolve each capture argument, falling back to what we can reconstruct
    # when replaying a legacy spool that lacks metadata.
    session_id = envelope.get("session_id") or (
        source.removeprefix("session:") if source.startswith("session:") else ""
    )

    if kind == "legacy":
        # Legacy spools predate the envelope and carry no metadata. We can
        # only replay as fleeting; warn so the user knows the classification
        # is degraded (this branch only fires for failures spooled by older
        # versions of triage — new failures use the envelope path).
        print(
            f"[memento] legacy spool detected for {source}; replaying as fleeting "
            "(original cwd/branch/files_edited/fleeting_only not preserved)",
            file=sys.stderr,
        )
        call_args = {
            "session_summary": envelope["session_summary"],
            "session_id": session_id,
            "agent": "claude",
            "fleeting_only": True,
        }
    else:
        # Full envelope — replay with the original args the first attempt used.
        call_args = {
            "session_summary": envelope["session_summary"],
            "cwd": envelope.get("cwd", ""),
            "branch": envelope.get("branch", ""),
            "files_edited": list(envelope.get("files_edited") or []),
            "session_id": session_id,
            "agent": envelope.get("agent", "claude"),
            "fleeting_only": bool(envelope.get("fleeting_only", True)),
        }

    result = capture(**call_args)

    if isinstance(result, dict) and "error" in result:
        return sync_ledger.record(
            vault, "capture", source,
            status="error",
            content_hash=entry.get("content_hash"),
            error=result["error"],
            spool_path=spool_path,
        )

    return sync_ledger.record(
        vault, "capture", source,
        status="ok",
        content_hash=entry.get("content_hash"),
        remote_path=(result or {}).get("path") if isinstance(result, dict) else None,
    )


def main():
    parser = argparse.ArgumentParser(description="Retry failed remote vault syncs.")
    parser.add_argument("--list", action="store_true", help="List pending retries without replaying.")
    parser.add_argument("--max", type=int, default=0, help="Max items to retry (0 = no limit).")
    args = parser.parse_args()

    if not is_remote():
        print("Remote vault not configured (MEMENTO_VAULT_URL unset). Nothing to retry.")
        return 0

    try:
        vault = get_vault()
    except Exception as exc:
        print(f"Could not locate vault: {exc}", file=sys.stderr)
        return 1

    pending = sync_ledger.pending_retries(vault)
    if not pending:
        print("No pending retries.")
        return 0

    if args.list:
        print(f"{len(pending)} pending retries:")
        for e in pending:
            print(f"  [{e.get('kind')}] {e.get('source')}  attempts={e.get('attempt')}  err={e.get('error', '')[:80]}")
        return 0

    if args.max and len(pending) > args.max:
        pending = pending[: args.max]

    ok = 0
    fail = 0
    for entry in pending:
        kind = entry.get("kind")
        if kind == "note":
            outcome = _retry_note(vault, entry)
        elif kind == "capture":
            outcome = _retry_capture(vault, entry)
        else:
            print(f"  Skip unknown kind: {kind}", file=sys.stderr)
            continue
        if outcome.get("status") == "ok":
            ok += 1
            print(f"  OK    [{kind}] {entry.get('source')} -> {outcome.get('remote_path', '?')}")
        else:
            fail += 1
            print(f"  FAIL  [{kind}] {entry.get('source')}: {outcome.get('error', '')[:120]}")

    print(f"\nDone: {ok} succeeded, {fail} failed.")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
