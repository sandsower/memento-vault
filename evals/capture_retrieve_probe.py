#!/usr/bin/env python3
"""Capture-then-retrieve loop probe (MEM-134).

Capture quality (capture_e2e.py) and retrieval accuracy (retrieval_probe.py)
are graded independently. Neither one asserts the thing a user actually
relies on: that a note captured *right now* can be found a moment later.
This probe closes that gap by driving the REAL pipeline end to end --
store, index, search -- against a hermetic temp vault.

Run as a subprocess by evals/suites/capture_retrieve_loop.py, exactly like
evals/retrieval_probe.py is run by evals/suites/retrieval_accuracy.py: this
script mutates the MEMENTO_VAULT_PATH env var, memento.config's process-wide
config cache, and the memento.search_backend singleton, so it must never run
inside the shared run_evals.py process (see retrieval_probe.py's module
docstring for the same rationale, MEM-133).

Modes
-----
--mode fixture
    Blocking, non-LLM layer: three pre-authored structured note payloads
    (fixture shapes mirroring what triage extraction produces -- see
    evals.suites.capture_e2e._validate_notes) pushed through the REAL
    smart_store/store MCP entry points (memento.mcp_server.memento_store /
    memento_store_smart) into a fresh temp vault, then a real
    EmbeddedSearchBackend (FTS5, no embedding provider, matching MEM-133's
    ranked-order probe) index build, then the real
    memento.search.qmd_search() entry point. Each case asserts its golden
    natural-language query returns the stored note in the top 5. This is
    the blocking CI gate: `python3 evals/capture_retrieve_probe.py --mode
    fixture` (wrapped by `python3 evals/run_evals.py --suite
    capture_retrieve_loop`).

--mode fixture --break-handoff
    Test-only (see tests/test_evals.py). Runs the same store -> index ->
    search pipeline for one case, but skips the explicit index-rebuild step
    after the store call -- simulating a host that writes a note to disk
    without telling the search index about it. Proves this probe actually
    detects a broken handoff instead of always finding notes through
    EmbeddedSearchBackend's empty-index auto-heal (see
    `_ensure_indexed()`'s `count == 0` branch in memento/embedded_search.py,
    and the module docstring on `run_case()` below for why a seed note is
    required to make the demonstration real). Never used by the blocking
    gate.

--mode llm
    Manual, opt-in layer. Feeds the SAME fixture transcripts capture_e2e.py
    uses through the real triage structured-notes prompt and the configured
    LLM backend (max 2 calls total: one insight-bearing transcript, one
    status-only transcript that must produce zero notes), grades the output
    with capture_e2e.py's existing deterministic rubric
    (`_validate_notes`), then pushes the insight transcript's first
    extracted note through the same store -> index -> search pipeline as
    `--mode fixture`. Skips cleanly (`{"skipped": true, "reason": ...}`)
    when memento.llm.preflight_check() reports no configured LLM backend --
    never a subprocess timeout or a stack trace.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

# --- Blocking layer 1 fixture cases ------------------------------------------
#
# Each case is engineered so the golden query shares almost no vocabulary
# with anything but the stored note (and, for the "title differs" case, not
# even the note's own title) -- so with only two documents ever in the
# per-case index (the neutral seed note plus the target note) top-5
# membership is unambiguous regardless of SQLite/FTS5 version (MEM-133 found
# real cross-platform ordering sensitivity; this suite only asserts
# membership, and keeps the corpus small enough that membership can't be a
# knife-edge call).

SEED_NOTE_TITLE = "Team standup notes template"
SEED_NOTE_BODY = "Agenda: yesterday, today, blockers. Keep updates under two minutes per person."

BLOCKING_CASES = [
    {
        "id": "typed_note_with_project_slug",
        "entry_point": "store",
        "query": "when does orbiter-api automatically roll back a canary deploy",
        "payload": {
            "title": "Canary rollback trigger threshold",
            "body": (
                "orbiter-api's deploy controller aborts a canary rollout and reverts to the "
                "previous revision automatically once the health-check error rate crosses 2% "
                "in the first five minutes of traffic, then pages the on-call engineer."
            ),
            "note_type": "decision",
            "tags": ["deploy", "canary", "orbiter-api"],
            "certainty": 4,
            "project": "/home/vic/Projects/orbiter-api",
        },
    },
    {
        "id": "session_note",
        "entry_point": "smart_store",
        "query": "why did the redis pool run out of connections during the billing migration",
        "payload": {
            "title": "Redis connection pool exhaustion during migration",
            "body": (
                "During the billing-service migration, the Redis client pool exhausted because "
                "the batch job opened one connection per row instead of reusing the pool; "
                "capping concurrent batch workers at 10 fixed it."
            ),
            "note_type": "bugfix",
            "tags": ["redis", "migration"],
            "certainty": 4,
            "session_id": "sess-mem134-fixture-002",
        },
    },
    {
        "id": "title_differs_from_query_wording",
        "entry_point": "store",
        "query": "what read timeout should the payments gateway http client use",
        "payload": {
            "title": "Notation for encoding retry backoff windows",
            "body": (
                "Set the HTTP client's read timeout to 15 seconds and the connection timeout to "
                "3 seconds for the payments gateway; anything longer causes the checkout queue "
                "to back up during a gateway outage."
            ),
            "note_type": "discovery",
            "tags": ["payments", "timeouts"],
            "certainty": 3,
        },
    },
]

# Reused by --break-handoff: any one payload/query pair would do, this is
# just the first blocking case.
BROKEN_HANDOFF_CASE = {**BLOCKING_CASES[0], "id": "broken_handoff_demo"}


def _make_temp_vault(root: Path) -> Path:
    vault = root / "vault"
    for d in ("notes", "fleeting", "projects", "archive"):
        (vault / d).mkdir(parents=True)
    return vault


def _activate_vault(vault: Path) -> None:
    """Point memento.config at the temp vault and drop cached config/vault
    state, mirroring retrieval_probe.render_fixture_vault's isolation."""
    from memento.config import reset_config

    os.environ["MEMENTO_VAULT_PATH"] = str(vault)
    os.environ["MEMENTO_SEARCH_BACKEND"] = "grep"
    os.environ.pop("MEMENTO_DEBUG", None)
    reset_config()


def _fts5_backend(vault: Path, db_path: Path):
    """A pure FTS5/BM25 EmbeddedSearchBackend: no embedding provider, so no
    vector table and no onnxruntime/sqlite-vec dependency at all -- same
    choice MEM-133 made for the ranked-order probe, for the same reason
    (determinism, no network/model dependency in a hermetic gate)."""
    from memento.embedded_search import EmbeddedSearchBackend

    return EmbeddedSearchBackend(vault_path=vault, db_path=db_path, embedding_provider=None)


def _seed_note(vault: Path) -> None:
    """Write one pre-existing, topically unrelated note through the real
    write_note() path before any case-specific store call.

    This exists so the broken-handoff demonstration is real rather than
    trivially defeated: EmbeddedSearchBackend._ensure_indexed() auto-heals
    (runs a full reindex) the first time it is asked to search an index with
    zero rows. Without a seed note, "skip the index build" would still find
    the target note on the very first search, because that first search
    would itself trigger the auto-heal and pick up everything on disk. With
    one already-indexed note in place, the index's row count is nonzero, the
    auto-heal never fires, and skipping the explicit rebuild after storing
    the target note produces a genuine miss -- exactly the class of bug
    (index not rebuilt after a capture) this suite exists to catch.
    """
    from memento.store import write_note

    write_note(
        vault,
        title=SEED_NOTE_TITLE,
        body=SEED_NOTE_BODY,
        note_type="pattern",
        tags=["process"],
        certainty=3,
        source="fixture",
    )


def _store_note(payload: dict, entry_point: str) -> dict:
    """Push a note through a REAL public MCP store entry point -- not a
    lower-level helper -- so this suite exercises exactly what an agent's
    write path calls."""
    from memento import mcp_server

    if entry_point == "store":
        return mcp_server.memento_store(**payload)
    if entry_point == "smart_store":
        return mcp_server.memento_store_smart(**payload)
    raise ValueError(f"unknown entry_point {entry_point!r}")


def run_case(case: dict, root: Path, *, skip_index_build: bool = False) -> dict:
    """Run one store -> index -> search case in its own temp vault.

    Stages, each using the real code path named:
      1. store   -- memento.mcp_server.memento_store / memento_store_smart
      2. index   -- memento.embedded_search.EmbeddedSearchBackend.reindex()
                    (skipped when skip_index_build=True)
      3. search  -- memento.search.qmd_search()
    """
    from memento import search, search_backend
    from memento.search_backend import GrepBackend

    vault = _make_temp_vault(root)
    _activate_vault(vault)

    # Neutral backend during writes: the real store path must not depend on
    # a live search index to succeed, and using Grep here (rather than the
    # EmbeddedSearchBackend under test) means the store step never
    # incidentally indexes anything -- indexing only happens at the explicit
    # "index build" stage below, which is the stage the broken-handoff
    # scenario skips.
    search_backend.set_backend(GrepBackend())
    _seed_note(vault)

    embedded = _fts5_backend(vault, root / ".search" / "search.db")
    embedded.reindex("memento")  # index build: seed note only, so far

    store_result = _store_note(case["payload"], case["entry_point"])
    if store_result.get("error"):
        return {
            "id": case["id"],
            "ok": False,
            "details": f"store failed via {case['entry_point']}: {store_result['error']}",
        }
    stored_path = store_result.get("path")
    if not stored_path:
        return {
            "id": case["id"],
            "ok": False,
            "details": f"store via {case['entry_point']} returned no path: {store_result}",
        }

    if not skip_index_build:
        embedded.reindex("memento")  # index build: now includes the just-stored note

    search_backend.set_backend(embedded)
    hits = search.qmd_search(case["query"], limit=5)
    top5 = [h["path"] for h in hits]
    found = stored_path in top5

    return {
        "id": case["id"],
        "ok": found,
        "details": (
            f"entry_point={case['entry_point']} query={case['query']!r} "
            f"stored_path={stored_path!r} top5={top5} skip_index_build={skip_index_build}"
        ),
    }


def run_fixture_mode(break_handoff: bool) -> list[dict]:
    tmp = Path(tempfile.mkdtemp(prefix="memento-eval-capture-retrieve-"))
    try:
        if break_handoff:
            return [run_case(BROKEN_HANDOFF_CASE, tmp / BROKEN_HANDOFF_CASE["id"], skip_index_build=True)]
        return [run_case(case, tmp / case["id"]) for case in BLOCKING_CASES]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- Manual layer 2: real triage extraction, then the same loop -------------

LLM_QUERY = "why does the exporter deadlock during the nightly vacuum"


def run_llm_mode(effective_now: str) -> dict:
    from memento.llm import preflight_check

    # Test-only override (mirrors MEMENTO_REGEN_GOLDEN / MEMENTO_EVAL_VECTOR_ADVISORY
    # in retrieval_probe.py): forces preflight_check() to report an unconfigured
    # backend deterministically, regardless of what CLI binaries happen to be
    # installed on the host machine. Never set by the blocking gate or run_evals.py.
    forced_backend = os.environ.get("MEMENTO_EVAL_FORCE_LLM_BACKEND")
    preflight_config = {"llm_backend": forced_backend} if forced_backend else None
    ok, message = preflight_check(preflight_config)
    if not ok:
        return {"effective_now": effective_now, "skipped": True, "reason": message}

    from evals.suites.capture_e2e import TRANSCRIPTS, _load_triage, _validate_notes
    from memento.adapters import render_transcript_text
    from memento.llm import llm_complete

    try:
        triage = _load_triage()
    except Exception as exc:
        return {"effective_now": effective_now, "skipped": False, "_error": f"triage import failed: {exc}"}

    kinds = [("insight", "insight-session.jsonl"), ("status", "status-only-session.jsonl")]
    extraction: dict = {}
    insight_notes: list = []
    for kind, filename in kinds:
        transcript_path = TRANSCRIPTS / filename
        try:
            meta = triage.parse_transcript(str(transcript_path))
            transcript_text = render_transcript_text(str(transcript_path))
        except Exception as exc:
            extraction[kind] = {"ok": False, "note_count": 0, "problems": [f"transcript parse failed: {exc}"]}
            continue
        prompt = triage._build_structured_notes_prompt(f"eval-{kind}", transcript_text, meta, "eval-fixture", [])
        try:
            response = llm_complete(
                prompt,
                config={
                    "llm_structured_json_schema": triage.TRIAGE_NOTES_JSON_SCHEMA,
                    "llm_structured_json_tool_name": "emit_notes",
                },
            )
            raw = response.text if hasattr(response, "text") else str(response)
        except Exception as exc:
            extraction[kind] = {"ok": False, "note_count": 0, "problems": [f"llm_complete failed: {str(exc)[:200]}"]}
            continue
        notes = triage._parse_structured_notes_response(raw)
        problems = _validate_notes(notes, kind)
        extraction[kind] = {"ok": not problems, "note_count": len(notes), "problems": problems}
        if kind == "insight":
            insight_notes = notes

    payload: dict = {"effective_now": effective_now, "skipped": False, "extraction": extraction}

    if not insight_notes or not extraction.get("insight", {}).get("ok"):
        payload["retrieval"] = {
            "ok": False,
            "details": "no well-formed insight note extracted; store/retrieve loop not attempted",
        }
        return payload

    note = insight_notes[0]
    root = Path(tempfile.mkdtemp(prefix="memento-eval-capture-retrieve-llm-"))
    try:
        case = {
            "id": "llm_insight_note",
            "entry_point": "store",
            "query": LLM_QUERY,
            "payload": {
                "title": str(note.get("title", "eval note"))[:200],
                "body": str(note.get("body", "")),
                "note_type": str(note.get("type") or "discovery"),
                "tags": list(note.get("tags") or []),
                "certainty": note.get("certainty"),
            },
        }
        result = run_case(case, root)
        payload["retrieval"] = {"ok": result["ok"], "details": result["details"]}
    finally:
        shutil.rmtree(root, ignore_errors=True)

    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["fixture", "llm"], required=True)
    parser.add_argument(
        "--break-handoff",
        action="store_true",
        help="test-only: skip the explicit index-build step to prove this probe detects a broken handoff",
    )
    parser.add_argument(
        "--now",
        help="ISO-8601 UTC timestamp to freeze the eval clock (env fallback: MEMENTO_EVAL_NOW)",
    )
    args = parser.parse_args()

    from evals.common import now as eval_now, set_now

    set_now(args.now)
    effective_now = eval_now().isoformat()

    if args.mode == "fixture":
        checks = run_fixture_mode(args.break_handoff)
        print(json.dumps({"effective_now": effective_now, "checks": checks}, sort_keys=True))
    else:
        if args.break_handoff:
            parser.error("--break-handoff only applies to --mode fixture")
        payload = run_llm_mode(effective_now)
        print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
