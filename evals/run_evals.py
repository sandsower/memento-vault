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
    vault_content       what we RECORDED: note structure, ephemerality, duplication
    capture_health      the WRITE path: failure rates, spend, anomalies (telemetry)
    retrieval_accuracy  the READ path: ranking policies, golden queries
    capture_e2e         the triage gate, plus LLM extraction with --llm

Everything is read-only. Only --llm spends tokens (2 LLM calls).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.common import FAIL, PASS, SKIP, WARN, find_vault  # noqa: E402
from evals.common import now as eval_now, set_now  # noqa: E402
from evals.suites import capture_e2e, capture_health, retrieval_accuracy, vault_content  # noqa: E402

SUITES = {
    "vault_content": vault_content,
    "capture_health": capture_health,
    "retrieval_accuracy": retrieval_accuracy,
    "capture_e2e": capture_e2e,
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

    has_fail = any(r.status == FAIL for r in results)
    has_warn = any(r.status == WARN for r in results)
    sys.exit(1 if has_fail or (args.strict and has_warn) else 0)


if __name__ == "__main__":
    main()
