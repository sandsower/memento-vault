#!/usr/bin/env python3
"""Memento quality evals: one command, one scorecard.

Usage:
    python3 evals/run_evals.py                  # all deterministic suites
    python3 evals/run_evals.py --suite vault_content
    python3 evals/run_evals.py --llm            # also run LLM extraction checks (spends tokens)
    python3 evals/run_evals.py --json           # machine-readable output
    python3 evals/run_evals.py --strict         # warnings also fail the exit code
    python3 evals/run_evals.py --now 2026-06-15T00:00:00Z  # freeze the eval clock
                                                 # (env fallback: MEMENTO_EVAL_NOW)

Exit codes: 0 = no failures (warnings allowed unless --strict), 1 = failures.

Suites:
    vault_content         what we RECORDED: note structure, ephemerality, duplication
    capture_health        the WRITE path: failure rates, spend, anomalies (telemetry)
    retrieval_accuracy    the READ path: ranking policies, golden queries
    capture_e2e           the triage gate, plus LLM extraction with --llm
    capture_retrieve_loop does a freshly captured note become retrievable? (store -> index -> search)

Everything is read-only against the real vault: capture_retrieve_loop writes only to its own
temp vaults, never the configured one. Only --llm spends tokens (up to 4 LLM calls total).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.common import FAIL, PASS, SKIP, WARN, find_vault, iter_notes  # noqa: E402
from evals.common import now as eval_now, set_now  # noqa: E402
from evals.suites import (  # noqa: E402
    capture_e2e,
    capture_health,
    capture_retrieve_loop,
    retrieval_accuracy,
    vault_content,
)

SUITES = {
    "vault_content": vault_content,
    "capture_health": capture_health,
    "retrieval_accuracy": retrieval_accuracy,
    "capture_e2e": capture_e2e,
    "capture_retrieve_loop": capture_retrieve_loop,
}

_ICONS = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}


def render_text(results):
    lines = []
    counts = {PASS: 0, WARN: 0, FAIL: 0, SKIP: 0}
    current_suite = None
    for r in results:
        if r.suite != current_suite:
            current_suite = r.suite
            lines.append("")
            lines.append(f"== {current_suite} ==")
        counts[r.status] += 1
        value = "" if r.value is None else f"  {r.value} {r.unit}".rstrip()
        lines.append(f"[{_ICONS[r.status]}] {r.title}{value}")
        if r.status in (WARN, FAIL):
            if r.threshold:
                lines.append(f"       threshold: {r.threshold}")
            for detail in r.details[:8]:
                lines.append(f"       - {detail}")
            if len(r.details) > 8:
                lines.append(f"       - ... {len(r.details) - 8} more (use --json for all)")
            if r.remediation:
                lines.append(f"       fix: {r.remediation}")
    lines.append("")
    lines.append(f"summary: {counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail, {counts[SKIP]} skip")
    return "\n".join(lines)


def build_baseline(results, effective_now, vault_note_count):
    """Shape a scorecard run into the small, git-diffable baseline schema
    (MEM-136): one entry per suite, one entry per check, sorted so that
    re-running against an unchanged vault produces an unchanged file.

    `known_gap` is derived from the check id, not carried on CheckResult
    itself: the known-gap aggregate checks the suites already emit
    (`<suite>.known_gaps`, `capture_e2e.gate_known_gaps`) are the only
    checks whose id ends in `known_gaps`, and their grade already encodes
    fixed-vs-open (PASS = all tracked gaps fixed, WARN = at least one
    still open). diff_baselines.py treats a WARN -> PASS transition on
    one of these as a promotion prompt rather than an ordinary improvement.
    """
    by_suite: dict[str, list] = {}
    for r in results:
        by_suite.setdefault(r.suite, []).append(r)

    suites = []
    for suite_name in sorted(by_suite):
        checks = [
            {
                "name": r.id,
                "grade": r.status,
                "value": r.value,
                "threshold": r.threshold,
                "known_gap": r.id.endswith("known_gaps"),
            }
            for r in sorted(by_suite[suite_name], key=lambda r: r.id)
        ]
        suites.append({"suite": suite_name, "checks": checks})

    return {
        "effective_now": effective_now,
        "vault_note_count": vault_note_count,
        "suites": suites,
    }


def _resolve_baseline_path(baseline_out: str, effective_now) -> Path:
    """--baseline-out accepts either a full file path (the ticket's own
    example: evals/baselines/2026-07-03.json) or a directory, in which case
    the filename date is derived from the effective eval clock (--now if
    given, else evals/common.now()) -- never a bare datetime.now()/date.today()
    call, so a weekly-scheduler invocation stays reproducible under --now."""
    path = Path(baseline_out).expanduser()
    if path.suffix == ".json" and not path.is_dir():
        return path
    return path / f"{effective_now.date().isoformat()}.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", choices=sorted(SUITES), action="append", help="run only these suites (repeatable)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--strict", action="store_true", help="warnings also fail the exit code")
    parser.add_argument("--llm", action="store_true", help="enable LLM extraction checks (spends tokens)")
    parser.add_argument("--vault", help="override vault path (default: memento config)")
    parser.add_argument(
        "--now",
        help="ISO-8601 UTC timestamp to freeze the eval clock for reproducible runs (env fallback: MEMENTO_EVAL_NOW)",
    )
    parser.add_argument(
        "--baseline-out",
        help="write a small baseline scorecard JSON (see evals/diff_baselines.py) to this path, or "
        "into this directory named <effective-clock-date>.json if the path is not a .json file",
    )
    args = parser.parse_args()

    set_now(args.now)
    effective_now = eval_now()
    # Propagate to subprocesses (e.g. retrieval_probe.py) that don't receive
    # an explicit --now, so the whole eval run shares one frozen clock.
    os.environ["MEMENTO_EVAL_NOW"] = effective_now.isoformat()

    context = {
        "vault": Path(args.vault).expanduser() if args.vault else find_vault(),
        "llm": args.llm,
        "now": effective_now.isoformat(),
    }

    selected = args.suite or list(SUITES)
    results = []
    for name in SUITES:
        if name not in selected:
            continue
        module = SUITES[name]
        try:
            results.extend(module.run(context))
        except Exception as exc:  # a crashing suite is itself a failing check
            from evals.common import CheckResult

            results.append(
                CheckResult(
                    id=f"{name}.crashed",
                    suite=name,
                    title=f"Suite {name} ran without crashing",
                    status=FAIL,
                    details=[f"{type(exc).__name__}: {exc}"],
                    remediation="Run the suite alone with --suite to get a traceback, "
                    'or `python3 -c "from evals.suites import %s as s; s.run({...})"`.' % name,
                )
            )

    if args.json:
        payload = {"effective_now": effective_now.isoformat(), "results": [r.to_dict() for r in results]}
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_text(results))

    if args.baseline_out:
        vault = context["vault"]
        vault_note_count = sum(1 for _ in iter_notes(vault)) if vault else 0
        baseline = build_baseline(results, effective_now.isoformat(), vault_note_count)
        baseline_path = _resolve_baseline_path(args.baseline_out, effective_now)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline written: {baseline_path}", file=sys.stderr)

    has_fail = any(r.status == FAIL for r in results)
    has_warn = any(r.status == WARN for r in results)
    sys.exit(1 if has_fail or (args.strict and has_warn) else 0)


if __name__ == "__main__":
    main()
