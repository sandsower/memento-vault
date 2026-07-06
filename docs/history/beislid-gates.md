# Beislið gate metadata

Memento Vault uses Beislið rich gate metadata in `.beislid/workflow.md`. The file preserves legacy `name` + `command` gates and adds selectors, stage labels, and retry hints for the full pre-PR proof set.

## Compatibility and migration

Flat gates are still valid Beislið input. A legacy gate such as:

```yaml
- name: release-smoke
  command: '.venv/bin/python scripts/release_smoke.py'
```

is interpreted as a pre-PR computational sensor with `mutates: false`. The rich form checked into this repo makes those defaults explicit and adds:

- `stage` — when the gate is relevant in the workflow. Current runnable repo gates remain `pre-pr` so existing review handoff still runs the same command set before push/PR.
- `kind` / `execution` — whether the item is a runnable computational sensor or declarative metadata for a future/human stage.
- `timeout_seconds` and `cost` — scheduling hints for unattended agents.
- `mutates` and `parallel_safe` — batching/safety hints. Gates marked `parallel_safe` must also be read-only and have no autofix step.
- `changed_file_selector` — advisory path metadata Rondo can use for automatic work planning and the full pre-PR gate catalog.
- `output.parser` — how an agent should summarize the result envelope.
- `failure` — retryability and stop/hint guidance for review-response or unattended repair loops.

Migration should preserve the original command text first, then add metadata. Do not move an existing required review gate to a non-`pre-pr` stage unless the repo intentionally wants current Beislið ready-for-review/review-response flows to stop executing it automatically.

## Changed-file-aware process artifact

Rondo's edit loop reads `.beislid/rondo-process-artifact.json` through `WORKFLOW.md`'s `process_provider.artifact_path`. That artifact uses `beislid-process-artifact-v1` and selector-backed `gate_sets` to pick post-turn gates from changed files. Rondo consumers should treat the gate list as a metadata source, not a run ledger.

The process artifact should:

- stay `status: approved`
- set `action_policy.decision: allow`
- use stable `action_id` values that line up with `.beislid/action-policy.json`
- keep selector-backed gates at `stage: post_turn`
- explain each selected gate with a concise `reason`
- explain skipped selector sets and unmatched paths in the selection metadata

This is the focused edit-loop selection surface. It is not a run ledger and it does not replace the full pre-PR gate catalog in `.beislid/workflow.md`.

## Stage policy for this repo

The supported Beislið stage vocabulary is `preflight`, `per-edit`, `pre-commit`, `pre-pr`, `post-pr`, `continuous`, and `human-interrupt`. Memento Vault currently uses rich `pre-pr` command gates for the required local proof before branch handoff. The post-turn process-artifact selector set is a separate Rondo editing surface; it should not replace the existing pre-PR proof until the configured handoff flow supports that lifecycle point.

## Doctor and Rondo expectations

`/doctor` should report these as staged rich gates and validate the metadata shape rather than warning because gates are no longer flat. It should treat flat gates as backward-compatible shorthand, warn only on invalid rich metadata, and note that P0 ready-for-review/review-response executes runnable computational `pre-pr` sensor gates.

Rondo consumers should treat `.beislid/workflow.md` as the durable full gate catalog and `.beislid/rondo-process-artifact.json` as the changed-file-aware subset used while editing. Selectors, costs, parsers, retry policies, and stage labels are planning/execution hints for deciding which local checks to run and how to summarize failures.
