# mem-145-run-lesson-ingest

Source: MEM-145
Approved: 2026-07-05T23:20:00Z by Vic Valenzuela <victor@dala.care>

## Objective
Rondo run evidence has a deterministic memento-side ingest path: a run-lesson payload becomes a vault note recallable by run id and ticket id, proven by an integration test.

## Scope
Include:
- `memento/automated_run_lessons.py`
- `memento/pi_bridge.py (new run-lesson CLI subcommand only)`
- `tests/test_script_harnesses.py or a new tests/test_run_lesson_ingest.py`
- `docs (ingest contract documentation)`

Exclude:
- Rondo-side emission (separate rondo ticket - note it in the final report)
- Automatic Pi lifecycle capture changes
- Diagnosing the 53 pi-bridge failures (report count if observed, do not fix)

## Autonomy
- Allow: edit included files, run gates/targeted tests, local commits.
- Ask: scope drift, new dependencies, behavior beyond the bound design.
- Deny: remote writes (push/PR/tracker), external mutations, destructive work outside the repo.

## Proof
- `.venv/bin/python -m ruff check .`
- `.venv/bin/python -m ruff format --check .`
- `.venv/bin/python -m compileall -q memento hooks scripts`
- `.venv/bin/python -m pytest tests/test_llm_backends.py tests/test_lifecycle.py tests/test_triage.py tests/test_store.py tests/test_frontmatter_schema.py tests/test_script_harnesses.py tests/test_beislid_workflow_gates.py`

## Pause conditions
Any need to change the lesson note schema beyond adding run_id/ticket_id fields to the payload contract; any pi_bridge CLI restructuring beyond adding one subcommand.

## Delivery
An operator or Rondo hook can run one documented command to ingest a run-lesson JSON; the resulting note embeds run id and ticket id verbatim in searchable text; an integration test proves search-by-run-id and search-by-ticket-id both recall it.
