# mem-124-queue-health-check

Source: MEM-124 (teotl unification P0 batch, audit finding M3).
Approved: 2026-07-02T17:10:23Z by Vic Valenzuela (explicit per-envelope verdict).

**Objective:** `memento health` reports capture-queue backlog size and oldest-entry age against config-overridable thresholds (WARN/FAIL).

**Scope:** `memento/health.py` + `tests/`; `pi_bridge.py` is a read-only reference for queue-path reuse.

**Autonomy:** supervised-auto; local branch allowed; push/PR denied; pi_bridge edits denied.

**Proof:** new backlogged-queue test + ruff check/format + compileall, all via `.venv/bin/python`.

**Pause on:** gate failure after retry, ambiguity, scope drift.

**Delivery:** summary incl. chosen threshold defaults, changed files, proof results; next step ready-for-review.

**Ordering:** first in the serial chain (mem-124 → mem-125 → mem-126; all three edit health.py).
