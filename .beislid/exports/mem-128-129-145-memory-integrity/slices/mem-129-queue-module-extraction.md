# mem-129-queue-module-extraction

Source: MEM-129
Approved: 2026-07-05T23:20:00Z by Vic Valenzuela <victor@dala.care>

## Objective
One queue module owns queue-path resolution and queue I/O behind the existing QueueStore protocol; pi_bridge, lifecycle, and health consume it; behavior is unchanged.

## Scope
Include:
- `a new memento/queue.py implementing the QueueStore protocol`
- `memento/capture_runtime.py (protocol home, read/extend)`
- `memento/pi_bridge.py (replace _queue_file/_legacy_queue_file/_PiQueueStore usage)`
- `memento/lifecycle.py (replace _pi_queue_file/_legacy_pi_queue_file/state-home logic)`
- `memento/health.py (replace its local _pi_queue_file)`
- `tests/test_capture_runtime.py`
- `tests/test_pi_bridge.py`
- `tests/test_lifecycle.py`
- `tests/test_health.py`

Exclude:
- build_session_context split (design-bearing - explicitly deferred, note as follow-up)
- Any capture/search/triage behavior change
- The 56-broad-excepts cleanup

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
Any observable behavior difference in queue paths or file formats; any need to touch capture/search/triage logic; ambiguity in legacy-queue-file migration semantics.

## Delivery
Queue-path resolution exists exactly once (memento/queue.py, implementing the QueueStore protocol from capture_runtime.py); pi_bridge/lifecycle/health delegate to it; MEMENTO_PI_STATE_HOME/XDG fallback semantics are byte-identical; all queue tests green.
