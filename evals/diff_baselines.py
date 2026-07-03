#!/usr/bin/env python3
"""Compare two evals/run_evals.py --baseline-out scorecards (MEM-136).

Usage:
    python3 evals/diff_baselines.py evals/baselines/2026-07-02.json evals/baselines/2026-07-09.json
    python3 evals/diff_baselines.py <old.json> <new.json> --json

Meant for a weekly habit (manual, or launchd/cron -- see evals/README.md):
run a fresh baseline, then diff it against last week's committed file. It
never runs the evals itself; feed it two files produced by
`evals/run_evals.py --baseline-out ...`.

Transition classes, one entry per affected check:

- new_fail       a check that did not exist in the old baseline at all
                 (a brand-new check) is FAIL in the new one.
- regression     an existing check's grade got worse (PASS -> WARN,
                 WARN -> FAIL, PASS -> FAIL, or a SKIP -> WARN/FAIL grade
                 appearing after a check starts producing values).
- improvement    an existing core check's grade got better.
- known_gap_fixed    a known_gap check flipped WARN (open) -> PASS (all
                 tracked gaps fixed) -- a promotion prompt: see "Promoting
                 a known-gap check" in evals/README.md.
- known_gap_reopened a known_gap check flipped PASS -> WARN (a previously
                 fixed gap regressed) -- also a regression.
- metric_delta   a check's numeric value moved by more than the noise
                 floor (evals/thresholds.yml: diffing.noise_floor,
                 default 0.02), independent of whether its grade changed.
- new_check / removed_check  informational only; a check appeared or
                 disappeared between the two baselines (suite selection
                 changed, or a check was added/removed in code).

Exit codes: 1 if any new_fail, regression, or known_gap_reopened entry is
present, 0 otherwise. Output is deterministic (sorted by suite, then check
name, then category) so two diffs of the same pair of files never differ.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.common import FAIL, PASS, SKIP, WARN, thresholds  # noqa: E402

_SEVERITY = {PASS: 0, SKIP: 0, WARN: 1, FAIL: 2}

REGRESSION_CATEGORIES = {"new_fail", "regression", "known_gap_reopened"}


def _noise_floor() -> float:
    node = thresholds().get("diffing", {})
    if isinstance(node, dict) and "noise_floor" in node:
        try:
            return float(node["noise_floor"])
        except (TypeError, ValueError):
            pass
    return 0.02


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _index(baseline: dict) -> dict:
    """suite -> check name -> check dict."""
    out: dict = {}
    for suite in baseline.get("suites", []):
        name = suite.get("suite")
        checks = {c["name"]: c for c in suite.get("checks", [])}
        out[name] = checks
    return out


def diff(old: dict, new: dict, noise_floor: float) -> list[dict]:
    """Return a sorted list of transition entries. Each entry:
    {suite, name, category, old_grade, new_grade, old_value, new_value, delta, known_gap}
    (fields not applicable to a category are None)."""
    old_idx = _index(old)
    new_idx = _index(new)
    suite_names = sorted(set(old_idx) | set(new_idx))

    entries = []
    for suite in suite_names:
        old_checks = old_idx.get(suite, {})
        new_checks = new_idx.get(suite, {})
        check_names = sorted(set(old_checks) | set(new_checks))
        for name in check_names:
            old_c = old_checks.get(name)
            new_c = new_checks.get(name)

            if new_c is None:
                entries.append(_entry(suite, name, "removed_check", old_c, new_c, old_c.get("known_gap", False)))
                continue
            if old_c is None:
                known_gap = new_c.get("known_gap", False)
                category = "new_fail" if new_c["grade"] == FAIL else "new_check"
                entries.append(_entry(suite, name, category, old_c, new_c, known_gap))
                continue

            known_gap = new_c.get("known_gap", old_c.get("known_gap", False))
            old_grade, new_grade = old_c["grade"], new_c["grade"]

            if old_grade != new_grade:
                if known_gap:
                    if old_grade == WARN and new_grade == PASS:
                        entries.append(_entry(suite, name, "known_gap_fixed", old_c, new_c, known_gap))
                    elif old_grade == PASS and new_grade == WARN:
                        entries.append(_entry(suite, name, "known_gap_reopened", old_c, new_c, known_gap))
                    elif _SEVERITY.get(new_grade, 0) > _SEVERITY.get(old_grade, 0):
                        entries.append(_entry(suite, name, "regression", old_c, new_c, known_gap))
                    elif _SEVERITY.get(new_grade, 0) < _SEVERITY.get(old_grade, 0):
                        entries.append(_entry(suite, name, "improvement", old_c, new_c, known_gap))
                else:
                    old_sev = _SEVERITY.get(old_grade, 0)
                    new_sev = _SEVERITY.get(new_grade, 0)
                    if new_sev > old_sev:
                        entries.append(_entry(suite, name, "regression", old_c, new_c, known_gap))
                    elif new_sev < old_sev:
                        entries.append(_entry(suite, name, "improvement", old_c, new_c, known_gap))

            old_val, new_val = old_c.get("value"), new_c.get("value")
            if _is_number(old_val) and _is_number(new_val):
                delta = new_val - old_val
                if abs(delta) > noise_floor:
                    entries.append(_entry(suite, name, "metric_delta", old_c, new_c, known_gap, delta=delta))

    entries.sort(key=lambda e: (e["suite"], e["name"], e["category"]))
    return entries


def _entry(suite, name, category, old_c, new_c, known_gap, delta=None):
    return {
        "suite": suite,
        "name": name,
        "category": category,
        "known_gap": bool(known_gap),
        "old_grade": (old_c or {}).get("grade"),
        "new_grade": (new_c or {}).get("grade"),
        "old_value": (old_c or {}).get("value"),
        "new_value": (new_c or {}).get("value"),
        "threshold": (new_c or old_c or {}).get("threshold", ""),
        "delta": delta,
    }


_LABELS = {
    "new_fail": "NEW FAIL",
    "regression": "REGRESSION",
    "improvement": "IMPROVEMENT",
    "known_gap_fixed": "KNOWN GAP FIXED",
    "known_gap_reopened": "KNOWN GAP REOPENED",
    "metric_delta": "METRIC DELTA",
    "new_check": "NEW CHECK",
    "removed_check": "REMOVED CHECK",
}


def render_text(entries: list[dict], noise_floor: float, old_path: str, new_path: str) -> str:
    lines = [f"diff: {old_path} -> {new_path}", f"noise floor: {noise_floor}"]
    current_suite = None
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["category"]] = counts.get(e["category"], 0) + 1
        if e["suite"] != current_suite:
            current_suite = e["suite"]
            lines.append("")
            lines.append(f"== {current_suite} ==")
        label = _LABELS.get(e["category"], e["category"].upper())
        detail = f"[{label}] {e['name']}"
        if e["category"] == "metric_delta":
            detail += f"  {e['old_value']} -> {e['new_value']} (delta {e['delta']:+.4f})"
        elif e["category"] in ("new_check", "removed_check"):
            detail += f"  grade={e['new_grade'] or e['old_grade']}"
        else:
            detail += f"  {e['old_grade']} -> {e['new_grade']}"
        if e["threshold"]:
            detail += f"  threshold: {e['threshold']}"
        if e["category"] == "known_gap_fixed":
            detail += "  -- promotion prompt: see 'Promoting a known-gap check' in evals/README.md"
        lines.append(detail)

    lines.append("")
    if not entries:
        lines.append("no changes")
    else:
        summary = ", ".join(f"{counts[c]} {_LABELS.get(c, c).lower()}" for c in sorted(counts))
        lines.append(f"summary: {summary}")

    regressions = [e for e in entries if e["category"] in REGRESSION_CATEGORIES]
    lines.append(f"regressions: {len(regressions)}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("old", help="older baseline JSON (evals/run_evals.py --baseline-out)")
    parser.add_argument("new", help="newer baseline JSON")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    old = _load(args.old)
    new = _load(args.new)
    noise_floor = _noise_floor()
    entries = diff(old, new, noise_floor)
    regressions = [e for e in entries if e["category"] in REGRESSION_CATEGORIES]

    if args.json:
        payload = {
            "old": args.old,
            "new": args.new,
            "noise_floor": noise_floor,
            "entries": entries,
            "regression_count": len(regressions),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_text(entries, noise_floor, args.old, args.new))

    sys.exit(1 if regressions else 0)


if __name__ == "__main__":
    main()
