# mem-125-fail-closed-health-reads

Source: MEM-125 (teotl unification P0 batch, audit finding M4).
Approved: 2026-07-02T17:10:23Z by Vic Valenzuela (explicit per-envelope verdict).

**Objective:** unreadable health inputs surface as WARN with the underlying error - never PASS, never crash the run. Four cited sites across `telemetry.py` and `health.py`.

**Scope:** `memento/telemetry.py`, `memento/health.py` + `tests/`.

**Autonomy:** supervised-auto; local branch allowed; push/PR denied.

**Proof:** four new unreadable-input tests (chmod 000 / missing file) + ruff check/format + compileall via `.venv/bin/python`.

**Pause on:** gate failure after retry, ambiguity, scope drift.

**Ordering:** second in the serial chain (after mem-124; both edit health.py).
