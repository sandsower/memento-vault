"""Vault content quality: is what we RECORD durable, atomic knowledge?

Every check here is deterministic and read-only. It scans the live vault
(notes/) and grades structural quality: frontmatter integrity, epistemic
metadata, project hygiene, ephemerality, duplication, and link health.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import timedelta

from evals.common import (
    CheckResult,
    FAIL,
    SKIP,
    grade,
    iter_notes,
    now as eval_now,
    parse_note,
    parse_note_date,
    pct,
    threshold,
)

SUITE = "vault_content"

REQUIRED_FIELDS = ("title", "type", "tags", "source", "date")
CANONICAL_TYPES = {"decision", "discovery", "pattern", "bugfix", "tool", "architecture", "daily"}

# Phrases that mark a note as transient run state rather than durable knowledge.
# Keep this list boring and literal so any agent can extend it.
EPHEMERAL_PATTERNS = [
    r"\bready for review\b",
    r"\bpr (was )?(opened|created|merged)\b",
    r"\bpr #\d+\b",
    r"\bhandoff state\b",
    r"\bverification (status|state|passed|baseline)\b",
    r"\bstatus before\b",
    r"\bstate (after|before)\b",
    r"\bmoved to (in review|in progress|done)\b",
    r"\bmarked (as )?ready\b",
    r"\bcommitted as `?[0-9a-f]{7,40}`?\b",
    r"\bbranch (was )?pushed\b",
    r"\bcurrent session\b",
]
_EPHEMERAL_RE = re.compile("|".join(EPHEMERAL_PATTERNS), re.IGNORECASE)

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _normalized_title(path):
    slug = path.stem.lower()
    slug = re.sub(r"^\d{4}-\d{2}-\d{2}[- ]*", "", slug)
    slug = re.sub(r"[- ]\d+$", "", slug)
    return slug


def run(context) -> list[CheckResult]:
    vault = context.get("vault")
    if vault is None:
        return [
            CheckResult(
                id="vault_content.vault_missing",
                suite=SUITE,
                title="Vault directory located",
                status=FAIL,
                remediation="Set MEMENTO_VAULT_PATH or run install.sh; no vault found.",
            )
        ]

    notes = []
    for path in iter_notes(vault):
        fm, body = parse_note(path)
        notes.append((path, fm, body))
    total = len(notes)
    if total == 0:
        return [
            CheckResult(
                id="vault_content.empty",
                suite=SUITE,
                title="Vault has notes to evaluate",
                status=SKIP,
                details=["notes/ is empty; nothing to grade"],
            )
        ]

    results = []
    known_stems = {p.stem.lower() for p, _, _ in notes}

    def check(metric, title, value, higher_is_better, unit="rate", details=None, remediation=""):
        warn = threshold(SUITE, metric, "warn", 0.9 if higher_is_better else 0.1)
        fail = threshold(SUITE, metric, "fail", 0.7 if higher_is_better else 0.3)
        results.append(
            CheckResult(
                id=f"{SUITE}.{metric}",
                suite=SUITE,
                title=title,
                status=grade(value, warn, fail, higher_is_better),
                value=value,
                unit=unit,
                threshold=f"warn {'<' if higher_is_better else '>'} {warn}, "
                f"fail {'<' if higher_is_better else '>'} {fail}",
                details=details or [],
                remediation=remediation,
            )
        )

    # --- frontmatter integrity -------------------------------------------
    parsed = [(p, fm, body) for p, fm, body in notes if fm is not None]
    check(
        "frontmatter_parse_rate",
        "Notes with parseable frontmatter",
        pct(len(parsed), total),
        True,
        details=[str(p.relative_to(vault)) for p, fm, _ in notes if fm is None],
        remediation="Repair or archive the listed notes; only the managed writer should create notes.",
    )

    with_required = [(p, fm, b) for p, fm, b in parsed if all(fm.get(f) for f in REQUIRED_FIELDS)]
    missing_examples = [
        f"{p.relative_to(vault)} missing {[f for f in REQUIRED_FIELDS if not fm.get(f)]}"
        for p, fm, _ in parsed
        if not all(fm.get(f) for f in REQUIRED_FIELDS)
    ]
    check(
        "required_fields_rate",
        "Notes carrying all required frontmatter fields",
        pct(len(with_required), total),
        True,
        details=missing_examples,
        remediation="Backfill missing fields or archive the notes; see docs/frontmatter-schema.md.",
    )

    # --- epistemic metadata ----------------------------------------------
    with_certainty = [(p, fm) for p, fm, _ in parsed if fm.get("certainty")]
    check(
        "certainty_present_rate",
        "Notes carrying a certainty value",
        pct(len(with_certainty), total),
        True,
        remediation="Certainty-less notes rank blind in retrieval; backfill via a defrag pass.",
    )

    invalid_certainty = []
    for p, fm in with_certainty:
        try:
            val = int(fm["certainty"])
            if not 1 <= val <= 5:
                invalid_certainty.append(f"{p.relative_to(vault)} certainty={val}")
        except ValueError:
            invalid_certainty.append(f"{p.relative_to(vault)} certainty={fm['certainty']!r}")
    check(
        "certainty_valid_rate",
        "Certainty values inside the 1-5 scale",
        pct(len(with_certainty) - len(invalid_certainty), len(with_certainty)) if with_certainty else None,
        True,
        details=invalid_certainty,
        remediation="Fix out-of-scale values (e.g. percent scores like 95); the scale is 1-5.",
    )

    # --- taxonomy ----------------------------------------------------------
    type_counts = Counter((fm.get("type") or "missing") for _, fm, _ in parsed)
    canonical = sum(count for t, count in type_counts.items() if t in CANONICAL_TYPES)
    non_canonical = [f"{t}: {c}" for t, c in type_counts.most_common() if t not in CANONICAL_TYPES]
    check(
        "canonical_type_rate",
        "Notes typed with a canonical note type",
        pct(canonical, total),
        True,
        details=non_canonical,
        remediation="Migrate legacy types (session, debugging, ...) to canonical ones; "
        "retrieval quality signals penalize untyped notes.",
    )

    # --- project hygiene ---------------------------------------------------
    with_project = [(p, fm) for p, fm, _ in parsed if fm.get("project")]
    slugged = [1 for _, fm in with_project if _SLUG_RE.match(fm["project"])]
    bad_projects = Counter(fm["project"] for _, fm in with_project if not _SLUG_RE.match(fm["project"]))
    check(
        "project_slug_rate",
        "Project values that are slugs rather than paths",
        pct(sum(slugged), len(with_project)) if with_project else None,
        True,
        details=[f"{v}: {c} notes" for v, c in bad_projects.most_common(15)],
        remediation="Normalize project to a stable slug at capture time; absolute paths and "
        "worktree dirs fragment project-scoped retrieval across machines.",
    )

    # --- ephemerality -------------------------------------------------------
    ephemeral = [p for p, fm, body in parsed if _EPHEMERAL_RE.search((fm.get("title") or "") + " " + body[:600])]
    check(
        "ephemeral_note_rate",
        "Notes that look like transient run state",
        pct(len(ephemeral), total),
        False,
        details=[str(p.relative_to(vault)) for p in ephemeral[:20]],
        remediation="Transient session state (PR opened, gates pending) belongs in fleeting/, "
        "not notes/. Tighten the triage prompt and archive the listed notes.",
    )

    # --- duplication ---------------------------------------------------------
    groups = defaultdict(list)
    for p, _, _ in notes:
        groups[_normalized_title(p)].append(p)
    dupe_groups = {k: v for k, v in groups.items() if len(v) > 1}
    duped_notes = sum(len(v) for v in dupe_groups.values())
    check(
        "near_duplicate_rate",
        "Notes in near-duplicate title groups",
        pct(duped_notes, total),
        False,
        details=[f"{len(v)}x {k}" for k, v in sorted(dupe_groups.items(), key=lambda i: -len(i[1]))[:15]],
        remediation="Merge or supersede duplicates; smart-store dedup should have caught these.",
    )

    # --- link health -----------------------------------------------------------
    linked = 0
    dangling = 0
    total_links = 0
    dangling_examples = []
    for p, _, body in notes:
        links = _WIKILINK_RE.findall(body)
        if links:
            linked += 1
        for link in links:
            total_links += 1
            target = link.strip().lower().replace(" ", "-")
            if target not in known_stems and link.strip().lower() not in known_stems:
                dangling += 1
                if len(dangling_examples) < 15:
                    dangling_examples.append(f"{p.relative_to(vault)} -> [[{link.strip()}]]")
    check(
        "wikilink_rate",
        "Notes with at least one outgoing wikilink",
        pct(linked, total),
        True,
        remediation="Unlinked notes are invisible to graph expansion; improve Related-section "
        "generation in the triage prompt or run inception to backfill links.",
    )
    check(
        "dangling_wikilink_rate",
        "Wikilinks resolving to a real note",
        pct(dangling, total_links) if total_links else None,
        False,
        details=dangling_examples,
        remediation="Fix or remove links to nonexistent notes; they waste graph-expansion hops.",
    )

    # --- size distribution -------------------------------------------------------
    stubs = [p for p, _, body in notes if len(body.strip()) < 200]
    oversized = [p for p, _, body in notes if len(body) > 8000]
    check(
        "stub_note_rate",
        "Notes with a body under 200 chars",
        pct(len(stubs), total),
        False,
        details=[str(p.relative_to(vault)) for p in stubs[:15]],
        remediation="Stubs carry no retrievable content; merge them into fuller notes or archive.",
    )
    check(
        "oversized_note_rate",
        "Notes with a body over 8000 chars",
        pct(len(oversized), total),
        False,
        details=[str(p.relative_to(vault)) for p in oversized[:15]],
        remediation="Oversized notes are usually transcript dumps; split into atomic notes.",
    )

    # --- supersedes integrity ------------------------------------------------------
    supersedes_total = 0
    supersedes_ok = 0
    broken = []
    for p, fm, _ in parsed:
        target = fm.get("supersedes")
        if not target:
            continue
        supersedes_total += 1
        cleaned = target.strip("[]\"' ").lower().replace(" ", "-")
        if cleaned in known_stems or cleaned.removesuffix(".md") in known_stems:
            supersedes_ok += 1
        elif len(broken) < 15:
            broken.append(f"{p.relative_to(vault)} supersedes {target!r}")
    check(
        "supersedes_integrity_rate",
        "Supersedes targets that resolve to an existing note",
        pct(supersedes_ok, supersedes_total) if supersedes_total else None,
        True,
        details=broken,
        remediation="Supersedes chains drive contradiction handling; validate targets at write time.",
    )

    # --- capture volume -------------------------------------------------------------
    now = eval_now()
    recent = sum(1 for _, fm, _ in parsed if (d := parse_note_date(fm)) and d >= now - timedelta(days=7))
    prior = sum(
        1
        for _, fm, _ in parsed
        if (d := parse_note_date(fm)) and now - timedelta(days=37) <= d < now - timedelta(days=7)
    )
    ratio = None
    if prior:
        ratio = round((recent / 7) / (prior / 30), 2)
    check(
        "growth_ratio",
        "Capture rate: last 7 days vs prior 30 days",
        ratio,
        False,
        unit="x",
        details=[f"last 7 days: {recent} notes", f"prior 30 days: {prior} notes"],
        remediation="A capture-rate spike usually means a backlog drain or a leaky triage gate "
        "flooding the vault; check triage_spawned telemetry for the same window.",
    )

    return results
