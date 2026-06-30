# Beislið gate metadata

Memento Vault uses Beislið's rich gate shape in `.beislid/workflow.md`. Each gate keeps the historical `name` and `command` fields and adds metadata that Rondo/Beislið orchestrators can consume when they need to select, schedule, summarize, or retry checks.

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
- `changed_file_selector` — advisory path metadata Rondo can use for automatic work planning and future gate-set selection.
- `output.parser` — how an agent should summarize the result envelope.
- `failure` — retryability and stop/hint guidance for review-response or unattended repair loops.

Migration should preserve the original command text first, then add metadata. Do not move an existing required review gate to a non-`pre-pr` stage unless the repo intentionally wants current Beislið ready-for-review/review-response flows to stop executing it automatically.

## Stage policy for this repo

The supported Beislið stage vocabulary is `preflight`, `per-edit`, `pre-commit`, `pre-pr`, `post-pr`, `continuous`, and `human-interrupt`. Memento Vault currently uses rich `pre-pr` command gates for the required local proof before branch handoff. Earlier or continuous stages may be introduced later as additional metadata or gate sets, but they should not replace the existing pre-PR proof until the configured handoff flow supports that lifecycle point.

## Doctor and Rondo expectations

`/doctor` should report these as staged rich gates and validate the metadata shape rather than warning because gates are no longer flat. It should treat flat gates as backward-compatible shorthand, warn only on invalid rich metadata, and note that P0 ready-for-review/review-response executes runnable computational `pre-pr` sensor gates.

Rondo consumers should treat the gate list as a metadata source, not a run ledger. The durable proof remains the actual gate result for the current branch. Selectors, costs, parsers, retry policies, and stage labels are planning/execution hints for deciding which local checks to run and how to summarize failures.
