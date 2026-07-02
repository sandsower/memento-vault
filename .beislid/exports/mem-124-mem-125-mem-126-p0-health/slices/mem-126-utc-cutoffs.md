# mem-126-utc-cutoffs

Source: MEM-126 (teotl unification P0 batch, audit finding M7).
Approved: 2026-07-02T17:10:23Z by Vic Valenzuela (explicit per-envelope verdict).

**Objective:** all health cutoffs computed tz-aware in UTC (`datetime.now(timezone.utc)` or the repo's tz helper) - six cited sites, plus any other naive-now cutoff found in `health.py`.

**Scope:** `memento/health.py` + `tests/`.

**Autonomy:** supervised-auto; local branch allowed; push/PR denied.

**Proof:** TZ-pinned window test (e.g. `Pacific/Kiritimati`, 23h-old record inside the 24h window) + ruff check/format + compileall via `.venv/bin/python`.

**Pause on:** gate failure after retry, ambiguity, scope drift.

**Ordering:** last in the serial chain (after mem-125; both edit health.py).
