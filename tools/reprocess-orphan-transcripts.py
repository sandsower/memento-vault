#!/usr/bin/env python3
"""Reprocess orphan Claude Code transcripts through memento-triage.

Usage:
    tools/reprocess-orphan-transcripts.py --list           # dry-run report
    tools/reprocess-orphan-transcripts.py --run            # reprocess all orphans
    tools/reprocess-orphan-transcripts.py --run --limit N  # reprocess first N

Temporarily disables auto_commit and inception_enabled while running so the
vault isn't spammed with 80+ single-session commits or inception triggers.
The caller commits the vault once at the end.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Make memento importable
_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo))

from memento.adapters import parse_transcript  # noqa: E402

TRIAGE_HOOK = Path.home() / ".claude" / "hooks" / "memento-triage.py"
TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"
RETRIEVAL_LOG = Path.home() / ".config" / "memento-vault" / "retrieval.jsonl"
CONFIG_PATH = Path.home() / ".config" / "memento-vault" / "memento.yml"
OVERSIZE_SIDECAR = Path.home() / ".config" / "memento-vault" / "oversize-orphans.txt"
# Sonnet's standard 200K context is ~800KB of text in the best case but
# code-heavy transcripts tokenize less favorably. 500KB keeps us clear of
# "prompt too long" rejections with headroom for the prompt scaffolding and
# the model's own response. Larger sessions go to the sidecar for later,
# chunked processing.
DEFAULT_MAX_KB = 500


def collect_triaged_session_ids() -> set[str]:
    """Return 8-char session_id prefixes that already have a triage decision.

    Triage logs the decision with session_id truncated to 8 chars, so we
    normalize every prefix and compare orphans on the same 8-char key.
    """
    triaged: set[str] = set()
    if not RETRIEVAL_LOG.exists():
        return triaged
    for line in RETRIEVAL_LOG.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("hook") == "triage" and rec.get("action") == "decision":
            sid = rec.get("session_id")
            if sid:
                triaged.add(sid[:8])
    return triaged


def collect_transcripts() -> list[tuple[str, Path]]:
    """Return [(session_id, transcript_path)] for every transcript on disk."""
    pairs: list[tuple[str, Path]] = []
    for path in TRANSCRIPT_ROOT.rglob("*.jsonl"):
        if "subagents" in path.parts:
            continue
        pairs.append((path.stem, path))
    return pairs


def inspect(path: Path) -> dict | None:
    try:
        return parse_transcript(str(path))
    except Exception:
        return None


def find_orphans(min_exchanges: int = 2, force: bool = False) -> list[dict]:
    """List orphan transcripts that are worth reprocessing.

    Orphan = no successful triage decision logged. We still honor the same
    exchange_count gate that triage would have applied, so we don't burn an
    LLM pass on empty sessions. When `force` is True, every eligible
    transcript is returned regardless of prior triage state.
    """
    triaged = set() if force else collect_triaged_session_ids()
    results: list[dict] = []
    for sid, path in collect_transcripts():
        if sid[:8] in triaged:
            continue
        meta = inspect(path)
        if meta is None:
            continue
        if meta.get("exchange_count", 0) < min_exchanges:
            continue
        results.append(
            {
                "session_id": sid,
                "path": str(path),
                "exchanges": meta["exchange_count"],
                "files_edited": len(meta.get("files_edited") or []),
                "cwd": meta.get("cwd"),
                "git_branch": meta.get("git_branch"),
                "size_kb": path.stat().st_size // 1024,
            }
        )
    results.sort(key=lambda r: r["exchanges"], reverse=True)
    return results


def split_by_size(orphans: list[dict], max_kb: int) -> tuple[list[dict], list[dict]]:
    """Partition orphans into (processable, oversize) by transcript size."""
    processable: list[dict] = []
    oversize: list[dict] = []
    for o in orphans:
        (oversize if o["size_kb"] > max_kb else processable).append(o)
    return processable, oversize


def write_oversize_sidecar(oversize: list[dict]) -> None:
    """Persist oversize orphans so they're not silently lost."""
    OVERSIZE_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Orphan transcripts too large for the current LLM context.",
        f"# Generated: {datetime.now().isoformat()}",
        "# Columns: session_id  size_kb  exchanges  files_edited  cwd",
        "",
    ]
    for o in oversize:
        lines.append(
            f"{o['session_id']}  {o['size_kb']}  {o['exchanges']}  "
            f"{o['files_edited']}  {o['cwd'] or '-'}"
        )
    OVERSIZE_SIDECAR.write_text("\n".join(lines) + "\n")


def _swap_config(disable_keys: dict[str, str]) -> str:
    """Rewrite memento.yml with a few top-level keys forced. Returns backup path."""
    if not CONFIG_PATH.exists():
        return ""
    backup = str(CONFIG_PATH) + f".bak.{int(time.time())}"
    shutil.copy2(CONFIG_PATH, backup)
    lines = CONFIG_PATH.read_text().splitlines()
    replaced: set[str] = set()
    new_lines = []
    for line in lines:
        matched = False
        for key, val in disable_keys.items():
            if line.startswith(f"{key}:") or line.startswith(f"{key} :"):
                new_lines.append(f"{key}: {val}")
                replaced.add(key)
                matched = True
                break
        if not matched:
            new_lines.append(line)
    for key, val in disable_keys.items():
        if key not in replaced:
            new_lines.append(f"{key}: {val}")
    CONFIG_PATH.write_text("\n".join(new_lines) + "\n")
    return backup


def _restore_config(backup: str) -> None:
    if backup and os.path.exists(backup):
        shutil.move(backup, CONFIG_PATH)


def _vault_root() -> Path:
    from memento.config import get_vault  # lazy: reads user config

    return Path(get_vault())


def _wait_for_sentinel(session_id: str, timeout: float) -> bool:
    """Wait until the structured-notes worker writes its sentinel.

    The worker is detached from triage, so the subprocess returning doesn't
    mean the LLM call finished. Pacing the backfill by sentinel keeps us from
    stampeding the claude CLI with 90+ concurrent processes.

    Not every decision spawns a worker — substantial sessions without the
    insight gate firing skip structured notes entirely and no sentinel ever
    lands. We cap the wait so those cases don't stall the whole backfill.
    """
    sentinel = _vault_root() / ".agent-done" / f"{session_id[:8]}.done"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sentinel.exists():
            return True
        time.sleep(1)
    return False


def _agent_was_spawned(session_id: str) -> bool:
    """Check retrieval.jsonl's tail for the triage decision we just made.

    Only decisions with agent_spawned=True fire a detached worker; the rest
    complete synchronously and don't need a sentinel wait.
    """
    if not RETRIEVAL_LOG.exists():
        return False
    try:
        lines = RETRIEVAL_LOG.read_text().splitlines()
    except OSError:
        return False
    prefix = session_id[:8]
    for line in reversed(lines[-200:]):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("hook") == "triage" and rec.get("action") == "decision" and rec.get("session_id") == prefix:
            return bool(rec.get("agent_spawned"))
    return False


def run_triage(orphan: dict, timeout: int = 300) -> tuple[bool, str]:
    hook_input = {
        "session_id": orphan["session_id"],
        "transcript_path": orphan["path"],
        "cwd": orphan.get("cwd") or "",
    }
    try:
        result = subprocess.run(
            [sys.executable, str(TRIAGE_HOOK)],
            input=json.dumps(hook_input),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "MEMENTO_AGENT": "claude"},
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout)[:300]
    if _agent_was_spawned(orphan["session_id"]):
        _wait_for_sentinel(orphan["session_id"], timeout=180)
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="Dry-run: print what would be processed")
    ap.add_argument("--run", action="store_true", help="Actually reprocess orphans")
    ap.add_argument("--limit", type=int, default=0, help="Stop after N orphans (0 = all)")
    ap.add_argument("--min-exchanges", type=int, default=2)
    ap.add_argument(
        "--max-kb",
        type=int,
        default=DEFAULT_MAX_KB,
        help=f"Skip transcripts larger than this (default {DEFAULT_MAX_KB} KB, logged to sidecar)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Reprocess even sessions that already have a triage decision logged",
    )
    args = ap.parse_args()

    if not (args.list or args.run):
        ap.error("Pass --list or --run")

    all_orphans = find_orphans(min_exchanges=args.min_exchanges, force=args.force)
    processable, oversize = split_by_size(all_orphans, args.max_kb)
    if oversize:
        write_oversize_sidecar(oversize)

    print(
        f"Found {len(all_orphans)} orphans total "
        f"(processable={len(processable)}, oversize>{args.max_kb}KB={len(oversize)}) "
        f"min_exchanges={args.min_exchanges}{' force' if args.force else ''}",
        file=sys.stderr,
    )
    if oversize:
        print(f"Oversize list written to {OVERSIZE_SIDECAR}", file=sys.stderr)

    orphans = processable[: args.limit] if args.limit else processable

    if args.list:
        for o in orphans:
            print(
                f"{o['session_id'][:8]}  kb={o['size_kb']:>6}  exch={o['exchanges']:>4}  "
                f"files={o['files_edited']:>3}  {o['cwd'] or '-'}"
            )
        return 0

    backup = _swap_config({"auto_commit": "false", "inception_enabled": "false"})
    ok = 0
    failed = 0
    start = time.time()
    try:
        for i, o in enumerate(orphans, 1):
            t0 = time.time()
            success, err = run_triage(o)
            dt = time.time() - t0
            tag = "OK" if success else "FAIL"
            line = f"[{i}/{len(orphans)}] {tag} {o['session_id'][:8]} exch={o['exchanges']} {dt:.1f}s"
            if not success:
                line += f" err={err.strip()[:160]}"
                failed += 1
            else:
                ok += 1
            print(line, file=sys.stderr, flush=True)
    finally:
        _restore_config(backup)

    elapsed = time.time() - start
    print(
        f"\nDone. ok={ok} failed={failed} elapsed={elapsed:.1f}s\n"
        f"Review vault changes, then commit manually.",
        file=sys.stderr,
    )
    print(datetime.now().isoformat(), file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
