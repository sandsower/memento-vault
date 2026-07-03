"""Capture-then-retrieve loop (MEM-134): does a freshly captured note become
retrievable?

capture_e2e.py grades capture quality; retrieval_accuracy.py grades
retrieval accuracy. Nothing in either suite asserts the actual user-facing
promise: store a note now, find it a moment later. This suite closes that
gap.

Two layers, both driven through evals/capture_retrieve_probe.py subprocesses
so temp-vault / search-backend-singleton / config-cache overrides never leak
into this process (same isolation rationale as retrieval_accuracy.py /
retrieval_probe.py, MEM-133):

1. Blocking, non-LLM (always runs): three pre-authored structured note
   payloads -- mirroring the shapes capture_e2e.py:_validate_notes grades --
   pushed through the real smart_store/store MCP entry points into a temp
   vault, a real EmbeddedSearchBackend (FTS5, no embedding provider) index
   build, then the real memento.search.qmd_search() entry point. Asserts
   each case's golden query returns the stored note in the top 5.
2. Manual, --llm tier: the same loop starting from a fixture transcript
   through real triage extraction (capture_e2e.py's fixtures and
   deterministic rubric, max 2 LLM calls total), then store, then retrieve.
   Skips cleanly when no LLM backend is configured.
"""

from __future__ import annotations

import json
import subprocess
import sys

from evals.common import (
    EVALS_DIR,
    CheckResult,
    FAIL,
    PASS,
    SKIP,
    grade,
    pct,
    threshold,
)

SUITE = "capture_retrieve_loop"
PROBE = EVALS_DIR / "capture_retrieve_probe.py"


def _run_probe(mode: str, now_iso: str | None = None, timeout: int = 120) -> dict | None:
    cmd = [sys.executable, str(PROBE), "--mode", mode]
    if now_iso:
        cmd += ["--now", now_iso]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        return {"_error": (proc.stderr or proc.stdout)[-500:]}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"_error": f"unparseable probe output: {proc.stdout[-300:]!r}"}


def run(context) -> list[CheckResult]:
    results = []
    now_iso = context.get("now")

    # ------------------------------------------------------------- blocking
    fixture = _run_probe("fixture", now_iso)
    if not fixture or "_error" in fixture:
        results.append(
            CheckResult(
                id=f"{SUITE}.probe",
                suite=SUITE,
                title="Capture-then-retrieve fixture probe ran",
                status=FAIL,
                details=[str((fixture or {}).get("_error", "no output"))],
                remediation="Run `python3 evals/capture_retrieve_probe.py --mode fixture` directly to see the traceback.",
            )
        )
        return results

    checks = fixture.get("checks", [])
    passed = [c for c in checks if c["ok"]]
    rate = pct(len(passed), len(checks))
    warn = threshold(SUITE, "blocking_recall_rate", "warn", 1.0)
    fail = threshold(SUITE, "blocking_recall_rate", "fail", 0.67)
    results.append(
        CheckResult(
            id=f"{SUITE}.blocking_recall_rate",
            suite=SUITE,
            title="A freshly captured note is retrievable via the real store -> index -> search path",
            status=grade(rate, warn, fail, higher_is_better=True),
            value=rate,
            unit="rate",
            threshold=f"warn < {warn}, fail < {fail}",
            details=[f"MISS {c['id']}: {c['details']}" for c in checks if not c["ok"]]
            or [f"{len(passed)}/{len(checks)} capture-then-retrieve cases found their note"],
            remediation="A stored note did not surface for its golden query. Check whether "
            "smart_store/store actually wrote it (memento/smart_store.py, memento/mcp_server.py) "
            "and whether the search index was rebuilt from the vault after the write "
            "(memento/embedded_search.py EmbeddedSearchBackend.reindex/index_note). This is "
            "exactly the gap MEM-134 exists to catch: capture quality and retrieval accuracy "
            "each passing their own evals does not mean a captured note is actually findable.",
        )
    )

    # -------------------------------------------------------------- manual
    if not context.get("llm"):
        results.append(
            CheckResult(
                id=f"{SUITE}.llm_loop",
                suite=SUITE,
                title="Capture-then-retrieve loop from real triage extraction (fixture transcript)",
                status=SKIP,
                details=["run with --llm to enable; makes up to 2 LLM calls via the configured backend"],
            )
        )
        return results

    llm = _run_probe("llm", now_iso, timeout=300)
    if not llm or "_error" in llm:
        results.append(
            CheckResult(
                id=f"{SUITE}.llm_loop",
                suite=SUITE,
                title="Capture-then-retrieve loop from real triage extraction (fixture transcript)",
                status=FAIL,
                details=[str((llm or {}).get("_error", "no output"))],
                remediation="Run `python3 evals/capture_retrieve_probe.py --mode llm` directly to see the traceback.",
            )
        )
        return results

    if llm.get("skipped"):
        results.append(
            CheckResult(
                id=f"{SUITE}.llm_loop",
                suite=SUITE,
                title="Capture-then-retrieve loop from real triage extraction (fixture transcript)",
                status=SKIP,
                details=[llm.get("reason", "no LLM backend configured")],
                remediation="Configure llm_backend (and any required credentials) in memento.yml to run this tier.",
            )
        )
        return results

    extraction = llm.get("extraction", {})
    extraction_problems = []
    for kind, info in extraction.items():
        if not info.get("ok"):
            extraction_problems.extend(f"{kind}: {p}" for p in info.get("problems", []))
    extraction_ok = sum(1 for info in extraction.values() if info.get("ok"))
    extraction_rate = pct(extraction_ok, len(extraction)) if extraction else None
    warn = threshold(SUITE, "llm_extraction_rate", "warn", 1.0)
    fail = threshold(SUITE, "llm_extraction_rate", "fail", 0.5)
    results.append(
        CheckResult(
            id=f"{SUITE}.llm_extraction_quality",
            suite=SUITE,
            title="Real triage extraction produces well-formed notes before the retrieval loop runs",
            status=grade(extraction_rate, warn, fail, higher_is_better=True),
            value=extraction_rate,
            unit="rate",
            threshold=f"warn < {warn}, fail < {fail}",
            details=extraction_problems or ["both fixture transcripts extracted cleanly"],
            remediation="Same rubric and thresholds as capture_e2e.llm_extraction (live-model output is "
            "not perfectly reproducible); see hooks/memento-triage.py.",
        )
    )

    retrieval = llm.get("retrieval", {})
    results.append(
        CheckResult(
            id=f"{SUITE}.llm_retrieval_hit",
            suite=SUITE,
            title="The note extracted from the insight fixture transcript is retrievable after real store + index",
            status=PASS if retrieval.get("ok") else FAIL,
            details=[retrieval.get("details", "no retrieval attempted")],
            remediation="Extraction succeeded but the stored note did not surface for the golden "
            "query -- the same class of bug as the blocking layer, reached via the LLM path "
            "instead of a fixture payload.",
        )
    )
    return results
