# Eval baselines

Append one entry per eval run (newest first) and one line per threshold change.
Run: `python3 evals/run_evals.py --json`.
See `evals/README.md` for the maintenance routine.

## Runs

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

## Threshold changes

(none yet)
