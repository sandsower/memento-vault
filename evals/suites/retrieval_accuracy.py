"""Retrieval accuracy: does the READ path find the right notes?

Two layers, both driven through evals/retrieval_probe.py subprocesses so
that vault/backend env overrides never leak into this process:

1. Hermetic policy checks (always run): a rendered fixture vault plus the
   grep backend exercises the real ranking-policy functions
   (temporal decay, quality signals, project scoping, archive exclusion).
2. Live golden queries (skipped if no backend): natural-language queries
   with known-correct notes in the user's real vault, graded as
   recall@5 and MRR, plus negative queries that must return nothing.
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
    WARN,
    grade,
    pct,
    threshold,
)

SUITE = "retrieval_accuracy"
PROBE = EVALS_DIR / "retrieval_probe.py"


def _run_probe(mode: str, now_iso: str | None = None) -> dict | None:
    cmd = [sys.executable, str(PROBE), "--mode", mode]
    if now_iso:
        cmd += ["--now", now_iso]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
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

    # ---------------------------------------------------- hermetic policy
    fixture = _run_probe("fixture", now_iso)
    if not fixture or "_error" in fixture:
        results.append(
            CheckResult(
                id=f"{SUITE}.policy_probe",
                suite=SUITE,
                title="Hermetic ranking-policy probe ran",
                status=FAIL,
                details=[str((fixture or {}).get("_error", "no output"))],
                remediation="Run `python3 evals/retrieval_probe.py --mode fixture` directly to see the traceback.",
            )
        )
    else:
        checks = fixture.get("checks", [])
        core = [c for c in checks if not c.get("known_gap")]
        gaps = [c for c in checks if c.get("known_gap")]
        core_passed = [c for c in core if c["ok"]]

        rate = pct(len(core_passed), len(core))
        warn = threshold(SUITE, "policy_pass_rate", "warn", 1.0)
        fail = threshold(SUITE, "policy_pass_rate", "fail", 0.75)
        results.append(
            CheckResult(
                id=f"{SUITE}.policy_pass_rate",
                suite=SUITE,
                title="Ranking-policy checks passing (fixture vault, real code)",
                status=grade(rate, warn, fail, higher_is_better=True),
                value=rate,
                unit="rate",
                threshold=f"warn < {warn}, fail < {fail}",
                details=[f"FAILED: {c['id']}: {c['details']}" for c in core if not c["ok"]]
                or [f"{len(core_passed)}/{len(core)} checks passed"],
                remediation="A regression in memento/search.py ranking policies; the failing "
                "check id names the behavior that broke.",
            )
        )

        gaps_fixed = [c for c in gaps if c["ok"]]
        results.append(
            CheckResult(
                id=f"{SUITE}.known_gaps",
                suite=SUITE,
                title="Known retrieval gaps fixed (tracked, informational)",
                status=PASS if len(gaps_fixed) == len(gaps) else WARN,
                value=f"{len(gaps_fixed)}/{len(gaps)}",
                unit="fixed",
                details=[f"{'FIXED' if c['ok'] else 'OPEN'}: {c['id']}: {c['title']}" for c in gaps],
                remediation="These encode desired behavior the pipeline does not implement "
                "yet (undated-note decay, slug project scoping, superseded-note demotion). "
                "When one flips to FIXED, move it to a core check in evals/retrieval_probe.py.",
            )
        )

    # ------------------------------------------------------- live golden set
    live = _run_probe("live", now_iso)
    if not live or "_error" in live:
        results.append(
            CheckResult(
                id=f"{SUITE}.live_probe",
                suite=SUITE,
                title="Live golden-query probe ran",
                status=FAIL,
                details=[str((live or {}).get("_error", "no output"))],
                remediation="Run `python3 evals/retrieval_probe.py --mode live` directly.",
            )
        )
        return results
    if not live.get("available"):
        results.append(
            CheckResult(
                id=f"{SUITE}.live_backend",
                suite=SUITE,
                title="Live search backend available",
                status=SKIP,
                details=[f"backend={live.get('backend')}"],
                remediation="Install/configure QMD or the embedded backend to grade live retrieval accuracy.",
            )
        )
        return results

    positives = live.get("positive", [])
    negatives = live.get("negative", [])
    backend_note = f"backend={live.get('backend')}"

    if positives:
        hits = [p for p in positives if p["rank"] is not None and p["rank"] <= 5]
        mrr = round(sum(1 / p["rank"] for p in positives if p["rank"]) / len(positives), 3)
        recall5 = pct(len(hits), len(positives))
        semantic_hits = [p for p in positives if p.get("semantic_rank") is not None]
        rescued = [p for p in positives if p["rank"] is None and p.get("semantic_rank")]
        warn = threshold(SUITE, "golden_recall_at_5", "warn", 0.7)
        fail = threshold(SUITE, "golden_recall_at_5", "fail", 0.4)
        results.append(
            CheckResult(
                id=f"{SUITE}.golden_recall_at_5",
                suite=SUITE,
                title="Golden queries answered in the top 5 (live vault)",
                status=grade(recall5, warn, fail, higher_is_better=True),
                value=recall5,
                unit="rate",
                threshold=f"warn < {warn}, fail < {fail}",
                details=[backend_note]
                + [f"MISS {p['id']}: top hits {p['got'][:3]}" for p in positives if p["rank"] is None or p["rank"] > 5],
                remediation="Golden queries are in evals/golden/retrieval_queries.json. A miss "
                "means either the note was archived/renamed (update expect_any) or search "
                "quality regressed (investigate before touching the golden file).",
            )
        )
        warn = threshold(SUITE, "golden_mrr", "warn", 0.6)
        fail = threshold(SUITE, "golden_mrr", "fail", 0.3)
        results.append(
            CheckResult(
                id=f"{SUITE}.golden_mrr",
                suite=SUITE,
                title="Mean reciprocal rank over golden queries (live vault)",
                status=grade(mrr, warn, fail, higher_is_better=True),
                value=mrr,
                unit="mrr",
                threshold=f"warn < {warn}, fail < {fail}",
                details=[backend_note] + [f"{p['id']}: rank {p['rank']}" for p in positives],
                remediation="Recall is fine but ranking is weak if recall@5 passes while MRR "
                "warns; look at quality signals and temporal decay weights.",
            )
        )
        results.append(
            CheckResult(
                id=f"{SUITE}.semantic_rescue",
                suite=SUITE,
                title="Semantic search rescues BM25 misses (informational)",
                status=PASS if not rescued else WARN,
                value=f"{len(rescued)} rescued, {len(semantic_hits)}/{len(positives)} semantic hits",
                unit="",
                details=[
                    f"{p['id']}: bm25 rank {p['rank']}, semantic rank {p.get('semantic_rank')} "
                    f"(top score {p.get('semantic_top_score')})"
                    for p in positives
                ],
                remediation="Rescued queries mean prompt recall (BM25-first, semantic only when "
                "warm) misses notes that vector search finds. Also compare semantic top scores "
                "with recall_min_score: thresholds calibrated for BM25 (0.9+) filter out valid "
                "semantic hits (0.5-0.7).",
            )
        )

    if negatives:
        clean = [n for n in negatives if not n["leaked"]]
        rate = pct(len(clean), len(negatives))
        warn = threshold(SUITE, "negative_query_pass_rate", "warn", 0.99)
        fail = threshold(SUITE, "negative_query_pass_rate", "fail", 0.7)
        results.append(
            CheckResult(
                id=f"{SUITE}.negative_query_pass_rate",
                suite=SUITE,
                title="Negative queries correctly return nothing above threshold",
                status=grade(rate, warn, fail, higher_is_better=True),
                value=rate,
                unit="rate",
                threshold=f"warn < {warn}, fail < {fail}",
                details=[f"LEAK {n['id']}: {n['leaked']}" for n in negatives if n["leaked"]],
                remediation="Leaks here become irrelevant injections in real sessions; raise "
                "recall_min_score or fix the leaking notes' tags.",
            )
        )

    return results
