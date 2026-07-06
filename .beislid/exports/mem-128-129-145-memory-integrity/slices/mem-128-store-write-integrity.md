# mem-128-store-write-integrity

Source: MEM-128
Approved: 2026-07-05T23:20:00Z by Vic Valenzuela <victor@dala.care>

## Objective
The vault store write path survives concurrency: locked dedup, unique tmp names, atomic project index, and frontmatter parsing that cannot fabricate fields from body content.

## Scope
Include:
- `memento/smart_store.py`
- `memento/store.py`
- `tests/test_store.py`
- `tests/test_smart_store.py`

Exclude:
- Queue lock (MEM-123)
- Search/scoring (MEM-127)
- Any behavior change beyond the four integrity fixes

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
Any acceptance criterion ambiguity (per ticket: stop and ask, do not invent behavior); any needed change outside the four named files.

## Delivery
Same-slug parallel writers cannot corrupt notes or the project index; a body '---' cannot fabricate frontmatter; rewrites round-trip unknown frontmatter keys; failing-first tests prove each fix.
