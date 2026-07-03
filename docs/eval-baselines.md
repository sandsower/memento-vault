# Eval baselines

As of MEM-136, baselines are machine-generated JSON files under `evals/baselines/`, one per run,
produced by `evals/run_evals.py --baseline-out evals/baselines/`.
Diff two of them with `evals/diff_baselines.py <old.json> <new.json>` -- see the "Weekly routine" and
"Baseline files and diffing" sections of `evals/README.md` for the full workflow (it is a local weekly
habit, not a CI job).

This file now exists to record **threshold changes** (still logged here, one line each, per the rule in
`evals/README.md`) and to preserve the prose history of runs from before the JSON baseline format
existed.
Do not append new run summaries here; commit the JSON file under `evals/baselines/` instead.

## History (pre-MEM-136 prose baselines)

### 2026-07-02 (initial baseline)

14 pass, 10 warn, 7 fail, 1 skip (capture_e2e LLM checks run separately: 1 pass, 2 warn).

Failures at baseline:

- `vault_content.project_slug_rate` 0.15 - project fields are absolute paths from two machines plus worktree dirs, not slugs.
- `vault_content.ephemeral_note_rate` 0.19 - one in five notes is transient run state (PR status, verification state, handoffs).
- `vault_content.growth_ratio` 4.65x - July capture-rate spike from the pi backlog drain.
- `capture_health.spawn_storm_ratio` 147x - 1473 triage spawns on 2026-07-01 alone.
- `capture_health.bridge_failures_per_day` 27.2 - pi-bridge status/tool-context failures (broken Python runtime on host).
- `retrieval_accuracy.golden_recall_at_5` 0.20 and `golden_mrr` 0.20 - natural-language golden queries mostly miss through the BM25-first recall path; semantic search rescued 5 of the 8 misses.

Known gaps open: undated-note decay, slug project scoping, superseded-note demotion, content-aware triage gating.

### 2026-07-03 (first JSON baseline: `evals/baselines/2026-07-03.json`)

Migrated to the machine-generated baseline format (MEM-136).
14 pass, 11 warn, 7 fail, 2 skip.
Same open failures and known gaps as 2026-07-02 (`project_slug_rate`, `ephemeral_note_rate`,
`growth_ratio`, `spawn_storm_ratio`, `bridge_failures_per_day`, `golden_recall_at_5`, `golden_mrr`), plus
one additional WARN (`capture_health.missing_transcript_rate` 0.0504, just over the 0.05 warn bound).
See the JSON file for exact values; this is the last prose-summarized run.

## Threshold changes

(none yet)
