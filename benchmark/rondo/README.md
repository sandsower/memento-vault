# Rondo benchmark task set

This directory contains a small, static benchmark set of real `memento-vault` GitHub issues for future Rondo and clean-evaluator work.

The fixture is intentionally data-only: it does not run Rondo, create worktrees, or call GitHub. A runner can consume `tasks.json`, select a task, give the issue snapshot and retrieval hints to an agent, then classify the result with the task rubric.

## Files

- `tasks.json` — machine-readable task definitions.
- `README.md` — schema and evaluation contract.

## Schema

`tasks.json` has `schema_version: 2` and a `tasks` array. Each task includes:

- `id`: stable benchmark task id, prefixed with `mv-`.
- `issue`: issue number, title, URL, labels, and body snapshot captured when the fixture was authored.
- `category`: one of the benchmark coverage categories:
  - `retrieval behavior`
  - `MCP/tooling`
  - `triage/capture`
  - `docs/process`
  - `sync/security/release`
- `difficulty`: coarse size signal for evaluators (`small`, `medium`, or `large`).
- `requires_vault_knowledge`: whether the task is expected to benefit from Memento Vault recall.
- `expected_touched_areas`: likely files/modules; not a required implementation path.
- `acceptance_criteria`: task-specific completion outcomes.
- `required_gates`: recommended verification commands or gate families.
- `known_risks`: issues an evaluator should watch for.
- `retrieval_context`:
  - `docs`: repo docs or source files worth reading.
  - `memento_queries`: recall prompts to run when vault knowledge is part of the task.
  - `expected_query_intent`: stable natural-language memory intent to score against without requiring exact note IDs.
  - `useful_note_refs`: stable note/doc refs when available; optional evaluation evidence, not the only success path.
  - `memory_token_budget`: benchmark memory budget in approximate tokens (`0` when vault memory is not expected).
- `rubric`:
  - `success`: what a successful run looks like.
  - `agent_failure`: symptoms caused by the coding agent's patch or reasoning.
  - `harness_failure`: symptoms caused by Rondo, the clean evaluator, environment, or unsupported host features.
  - `ambiguous_requirements`: acceptable ambiguity in the originating issue.

## Evaluation notes

A clean evaluator should distinguish three outcomes:

1. **Agent failure** — the agent had enough information and tools, but produced an incomplete, unsafe, or failing change.
2. **Harness failure** — the patch may be reasonable, but Rondo/evaluator infrastructure could not apply it, run required host capabilities, or surface logs correctly.
3. **Ambiguous requirements** — the issue admits more than one reasonable implementation. These tasks should not be scored as hard failures when the agent documents assumptions and satisfies a coherent interpretation.

The issue snapshots are deliberately static so benchmark runs remain reproducible even if live GitHub issues are edited later. Refreshing snapshots should be done in a normal reviewable PR.

## Memory outcome report

External runners can emit compact sanitized outcome JSON/JSONL and classify it with:

```bash
python benchmark/rondo/evaluation.py --outcomes /path/to/outcomes.jsonl --output /tmp/rondo-memory-report.json
```

Accepted outcome summaries include fields such as `task_id`, `memory_used`, `retrieval_queries`, `retrieved_note_refs`, `retrieval_latency_ms`, and `memory_token_budget`. Raw logs, transcripts, proofs, run ledgers, and stdout/stderr dumps are rejected: Memento evaluates memory contribution but must not become a run ledger.

The report classifies required-memory tasks as `used_relevant_memory`, `memory_not_retrieved`, or `irrelevant_memory`. Relevance can be measured through `expected_query_intent` overlap even when exact `useful_note_refs` are absent or unstable.

`memento-vault retrieval-report --benchmark-outcomes /tmp/rondo-memory-report.json` can include these benchmark classifications alongside local retrieval diagnostics.
