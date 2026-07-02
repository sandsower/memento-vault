"""Capture end-to-end: does the triage pipeline keep the right sessions and
write well-formed, durable notes?

Two layers:

1. Triage-gate checks (always run, no LLM): labeled session metadata through
   the real is_substantial() from hooks/memento-triage.py. Guards against
   threshold regressions that would flood or starve the vault.
2. LLM extraction checks (only with --llm): fixture transcripts through the
   real structured-notes prompt and the configured LLM backend, with the
   output graded DETERMINISTICALLY (schema validity, canonical type,
   certainty range, ephemeral-language scan). This is the only part of the
   eval suite that spends tokens; it makes one LLM call per fixture
   transcript (two total).
"""

from __future__ import annotations

import importlib.util
import sys

from evals.common import (
    EVALS_DIR,
    REPO_ROOT,
    CheckResult,
    FAIL,
    PASS,
    SKIP,
    WARN,
    grade,
    pct,
    threshold,
)
from evals.suites.vault_content import CANONICAL_TYPES, _EPHEMERAL_RE

SUITE = "capture_e2e"
TRANSCRIPTS = EVALS_DIR / "golden" / "fixtures" / "transcripts"


def _load_triage():
    spec = importlib.util.spec_from_file_location("memento_triage_eval", str(REPO_ROOT / "hooks" / "memento-triage.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["memento_triage_eval"] = module
    spec.loader.exec_module(module)
    return module


# Labeled gate cases: (case id, meta, should the triage gate keep it?, known_gap)
def _gate_cases():
    base = {"files_edited": [], "files_read": [], "first_prompt": "", "exchange_count": 0}
    return [
        (
            "trivial_qa",
            {**base, "exchange_count": 2, "first_prompt": "what does this flag do"},
            False,
            False,
        ),
        (
            "long_design_session",
            {
                **base,
                "exchange_count": 20,
                "files_edited": ["/p/plan.md", "/p/a.py", "/p/b.py", "/p/c.py"],
                "first_prompt": "design the new sync engine",
            },
            True,
            False,
        ),
        (
            "bugfix_session",
            {
                **base,
                "exchange_count": 6,
                "files_edited": ["/p/fix.py"],
                "first_prompt": "why does the exporter crash at 02:00, find the root cause",
            },
            True,
            False,
        ),
        (
            "read_heavy_deep_dive",
            {
                **base,
                "exchange_count": 4,
                "files_read": [f"/p/{i}.py" for i in range(7)],
                "first_prompt": "explain how the retry pipeline works",
            },
            True,
            False,
        ),
        # A long babysit/status session has many exchanges but produces no
        # durable knowledge. The gate only counts exchanges, so it keeps it.
        # Desired: content-aware gating drops it.
        (
            "status_babysit_session",
            {
                **base,
                "exchange_count": 18,
                "first_prompt": "keep an eye on the release PR and tell me when CI is green",
            },
            False,
            True,
        ),
    ]


def _validate_notes(notes, transcript_kind):
    """Deterministic rubric for LLM-extracted notes. Returns list of problems."""
    problems = []
    for i, note in enumerate(notes):
        label = f"note[{i}] {str(note.get('title', ''))[:50]!r}"
        for field in ("title", "body", "type", "tags", "certainty"):
            if not note.get(field) and note.get(field) != 0:
                problems.append(f"{label}: missing {field}")
        note_type = str(note.get("type", ""))
        if note_type and note_type not in CANONICAL_TYPES:
            problems.append(f"{label}: non-canonical type {note_type!r}")
        certainty = note.get("certainty")
        if not isinstance(certainty, int) or not 1 <= certainty <= 5:
            problems.append(f"{label}: certainty {certainty!r} not an int in 1-5")
        text = f"{note.get('title', '')} {note.get('body', '')}"
        if _EPHEMERAL_RE.search(text):
            problems.append(f"{label}: contains ephemeral run-state language")
        if len(str(note.get("body", ""))) > 4000:
            problems.append(f"{label}: body over 4000 chars, likely a transcript dump")
    if transcript_kind == "insight" and not notes:
        problems.append("insight transcript produced zero notes")
    if transcript_kind == "status" and notes:
        problems.append(f"status-only transcript produced {len(notes)} notes; expected none")
    return problems


def run(context) -> list[CheckResult]:
    results = []
    try:
        triage = _load_triage()
    except Exception as exc:
        return [
            CheckResult(
                id=f"{SUITE}.import",
                suite=SUITE,
                title="Triage module imports",
                status=FAIL,
                details=[str(exc)[:300]],
                remediation="hooks/memento-triage.py failed to import; capture is likely broken "
                "everywhere, not just in the eval.",
            )
        ]

    # ------------------------------------------------------------ gate checks
    core_ok, core_total = 0, 0
    gap_lines = []
    failures = []
    for case_id, meta, expected, known_gap in _gate_cases():
        actual = bool(triage.is_substantial(meta))
        if known_gap:
            gap_lines.append(
                f"{'FIXED' if actual == expected else 'OPEN'}: {case_id} (want keep={expected}, got keep={actual})"
            )
            continue
        core_total += 1
        if actual == expected:
            core_ok += 1
        else:
            failures.append(f"{case_id}: want keep={expected}, got keep={actual}")

    results.append(
        CheckResult(
            id=f"{SUITE}.triage_gate_accuracy",
            suite=SUITE,
            title="Triage substantiality gate classifies labeled sessions correctly",
            status=PASS if core_ok == core_total else FAIL,
            value=pct(core_ok, core_total),
            unit="rate",
            threshold="all labeled cases must classify correctly",
            details=failures or [f"{core_ok}/{core_total} cases correct"],
            remediation="Thresholds in memento.yml (exchange_threshold, file_count_threshold) "
            "or is_substantial() heuristics changed behavior; check hooks/memento-triage.py.",
        )
    )
    results.append(
        CheckResult(
            id=f"{SUITE}.gate_known_gaps",
            suite=SUITE,
            title="Known triage-gate gaps fixed (tracked, informational)",
            status=PASS if all(line.startswith("FIXED") for line in gap_lines) else WARN,
            value=f"{sum(1 for line in gap_lines if line.startswith('FIXED'))}/{len(gap_lines)}",
            unit="fixed",
            details=gap_lines,
            remediation="The gate keeps long status/babysit sessions because it only counts "
            "exchanges; content-aware gating would fix this. When fixed, promote the case.",
        )
    )

    # ------------------------------------------------------- LLM extraction
    if not context.get("llm"):
        results.append(
            CheckResult(
                id=f"{SUITE}.llm_extraction",
                suite=SUITE,
                title="LLM extraction quality (fixture transcripts)",
                status=SKIP,
                details=["run with --llm to enable; makes 2 LLM calls via the configured backend"],
            )
        )
        return results

    from memento.llm import llm_complete

    kinds = [("insight", "insight-session.jsonl"), ("status", "status-only-session.jsonl")]
    batch_ok = 0
    all_problems = []
    for kind, filename in kinds:
        transcript_path = TRANSCRIPTS / filename
        try:
            from memento.adapters import render_transcript_text

            meta = triage.parse_transcript(str(transcript_path))
            transcript_text = render_transcript_text(str(transcript_path))
        except Exception as exc:
            all_problems.append(f"{kind}: transcript parse failed: {exc}")
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
            all_problems.append(f"{kind}: llm_complete failed: {str(exc)[:200]}")
            continue
        notes = triage._parse_structured_notes_response(raw)
        problems = _validate_notes(notes, kind)
        if problems:
            all_problems.extend(f"{kind}: {p}" for p in problems)
        else:
            batch_ok += 1

    rate = pct(batch_ok, len(kinds))
    warn = threshold(SUITE, "schema_valid_rate", "warn", 1.0)
    fail = threshold(SUITE, "schema_valid_rate", "fail", 0.5)
    results.append(
        CheckResult(
            id=f"{SUITE}.llm_extraction",
            suite=SUITE,
            title="LLM extraction produces well-formed, durable notes (fixtures)",
            status=grade(rate, warn, fail, higher_is_better=True),
            value=rate,
            unit="rate",
            threshold=f"warn < {warn}, fail < {fail}",
            details=all_problems or ["both fixture transcripts extracted cleanly"],
            remediation="Failures name the exact rubric violation. Ephemeral language in the "
            "status fixture means the triage prompt does not distinguish durable knowledge "
            "from run state; tighten the prompt in hooks/memento-triage.py.",
        )
    )
    return results
