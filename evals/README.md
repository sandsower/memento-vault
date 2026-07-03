# Memento quality evals

One command that grades the whole memory system: what gets recorded (input quality), what gets found (retrieval accuracy), what the pipeline costs (model use and token spend), and whether the machinery is healthy.

```bash
python3 evals/run_evals.py              # all deterministic suites, ~1-2 minutes, zero tokens
python3 evals/run_evals.py --llm        # adds LLM extraction checks (exactly 2 LLM calls)
python3 evals/run_evals.py --json       # machine-readable scorecard
python3 evals/run_evals.py --strict     # warnings also fail the exit code
python3 evals/run_evals.py --suite vault_content   # one suite only (repeatable flag)
python3 evals/run_evals.py --now 2026-06-15T00:00:00Z   # freeze the clock (env fallback: MEMENTO_EVAL_NOW)
```

Exit code 0 means no failures.
Everything is read-only against the vault and telemetry logs.
Only `--llm` spends tokens.
`--now` freezes every suite's time-window math (growth ratios, telemetry windows, retrieval-fixture dates) to one instant, so two runs against the same fixture vault with the same `--now` produce byte-identical JSON output.
The effective clock is echoed back as `effective_now` in `--json` output.

## How to read the scorecard

Every check prints a status, a value, the threshold it was graded against, up to 8 offending examples, and a `fix:` line saying what to do about it.

- `PASS` - healthy.
- `WARN` - degrading; investigate when convenient.
- `FAIL` - actively hurting memory quality; fix soon.
- `SKIP` - the check could not run (missing backend, no telemetry); the details say why.

Checks labeled `informational` or `known gaps` never fail the run.
Known-gap checks encode desired behavior the system does not implement yet (for example: superseded notes are not demoted in ranking).
They exist so progress is visible: when one flips to FIXED, promote it to a core check (see maintenance below).

## The five suites

### vault_content - what we recorded

Scans every note in the vault and grades structure and substance: frontmatter validity, certainty presence and scale, canonical types, project-field hygiene, ephemeral run-state notes, near-duplicates, wikilink health, note sizes, supersedes integrity, and capture-volume spikes.
This is the input-quality suite: if capture writes junk, it shows up here first.

### capture_health - the write path

Parses `~/.config/memento-vault/triage-health.jsonl` over a 30-day window (tunable in `thresholds.yml`).
Grades LLM failure rate, parse-empty rate, missing transcripts, transcript truncation, notes-per-attempt yield, spawn storms, and pi-bridge failures.
Also prints an informational spend report: LLM calls, prompt/output bytes, token totals where available, average duration, and backend/model mix.
Token counts are only recorded for API backends; CLI backends (claude, codex) report bytes and duration only.

### retrieval_accuracy - the read path

Two layers:

1. Hermetic policy checks run the real ranking functions from `memento/search.py` against a rendered fixture vault (`golden/fixtures/vault/`) with the grep backend forced, in a subprocess so nothing leaks.
They verify temporal decay, certainty immunity, session-type and low-certainty penalties, fleeting-path dropping, project scoping, and archive exclusion.
This layer also includes a golden **ranked-order** regression (`ranked_order_checks()` in `retrieval_probe.py`, MEM-133): for a fixed set of queries it runs the real FTS5/BM25 + PRF + RRF fusion + policy path (embedded backend, no embedding provider, so no vector index and no ONNX/network dependency) and asserts the exact top-5 note-path order against `golden/ranked_order.json`.
Where the pairwise checks above only prove a signal moves scores in the right *direction*, this proves a real query's *order* has not silently shifted -- the gap a fusion or reranker weight change can slip through.
When the `embedded` extra's dependencies are installed AND the local ONNX embedding model is already cached AND `MEMENTO_EVAL_VECTOR_ADVISORY=1` is set, the same queries also run through the embedded backend with a real embedding provider (FTS5 + vector, RRF-fused) and the resulting order is printed under the `vector_advisory` JSON key for information only; it is opt-in (ONNX CPU inference is not bit-reproducible run to run) and never gates anything.
2. Live golden queries run natural-language questions from `golden/retrieval_queries.json` against the real vault and backend, grading recall@5, MRR, and negative-query leakage.
Each positive query is also run through semantic search so the scorecard shows when vector search would have rescued a BM25 miss.

Layer 1 is a blocking CI gate: `python3 evals/retrieval_probe.py --mode fixture --strict` exits non-zero if any core (non-known-gap) check fails -- this includes the ranked-order checks.
Layer 2 never runs in CI; it needs the real vault.

### capture_e2e - the triage gate and extraction

Always runs the real `is_substantial()` gate against labeled fixture sessions.
With `--llm`, feeds two fixture transcripts (`golden/fixtures/transcripts/`) through the real structured-notes prompt and the configured LLM backend, then grades the output deterministically: schema validity, canonical type, certainty in 1-5, no ephemeral run-state language, and, crucially, that the status-only transcript produces zero notes.
No LLM judge is used; the rubric is plain string and schema checks so results are reproducible.

The hermetic layer is a blocking CI gate: `python3 evals/run_evals.py --suite capture_e2e` (without `--llm`, so the extraction layer is skipped and no tokens are spent).

### capture_retrieve_loop - does a captured note become retrievable?

capture_e2e grades capture quality; retrieval_accuracy grades retrieval accuracy.
Neither asserts the thing a user actually relies on: that a note captured right now can be found a moment later.
This suite drives the real pipeline end to end -- store, index, search -- against a hermetic temp vault (`evals/capture_retrieve_probe.py`, run as a subprocess for the same isolation reasons as `retrieval_probe.py`).

Two layers:

1. Blocking, non-LLM: three pre-authored structured note payloads (fixture shapes mirroring what triage extraction produces) pushed through the real `memento_store`/`memento_store_smart` MCP entry points into a temp vault, a real `EmbeddedSearchBackend` (FTS5, no embedding provider) index build, then the real `memento.search.qmd_search()` entry point.
Each case asserts its golden query returns the stored note in the top 5: a typed note carrying a project slug, a note carrying session metadata, and a note whose title shares no vocabulary with its query (forcing a content match, not a title match).
2. Manual, `--llm` tier: the same loop starting from capture_e2e's insight fixture transcript through real triage extraction (max 2 LLM calls, graded by capture_e2e's existing deterministic rubric), then store, then retrieve.
Skips cleanly when no LLM backend is configured.

The blocking layer is a blocking CI gate: `python3 evals/run_evals.py --suite capture_retrieve_loop` (writes only to its own temp vaults, never the configured one).

## Maintenance guide

This section is written for any agent, including ones weaker than the one that built this.
Follow it mechanically; no design judgment is needed for routine upkeep.

### Weekly routine

1. Run `python3 evals/run_evals.py --json > /tmp/evals.json` and read the summary.
2. Compare against the previous run (keep them in `docs/eval-baselines.md`, newest first, one line per run: date, pass/warn/fail counts, and any metric that changed status).
3. For every NEW failure, read the `fix:` line and the details.
Fix the cause, never the threshold.
4. Once a month, run with `--llm` to check extraction quality (2 LLM calls).

### When a golden query starts missing

First check whether the expected note was renamed, archived, or superseded: `rg <slug> ~/memento/notes ~/memento/archive`.

- Renamed or superseded: update `expect_any` in `golden/retrieval_queries.json` to the new path substring.
- Still exists but not found: retrieval regressed.
Do not touch the golden file; investigate the search pipeline (start with `memento-vault retrieval-report`).

### When a ranked-order golden check starts failing

This means the exact top-5 order for one of the fixed queries in `RANKED_ORDER_QUERIES` (`retrieval_probe.py`) changed.
Treat that as a real signal, not noise: something in FTS5/BM25 relevance, PRF expansion, RRF fusion, temporal decay, or quality signals moved.

1. Run `python3 evals/retrieval_probe.py --mode fixture` directly and read the failing check's `details` (`expected` vs `got`).
2. Decide whether the new order is *better*, *worse*, or *neutral* given the fixture scenario the query was engineered for (each query's comment in `RANKED_ORDER_QUERIES` names the scenario).
3. If the change was intentional (you changed a ranking parameter or fixed a bug), regenerate: `MEMENTO_REGEN_GOLDEN=1 python3 evals/retrieval_probe.py --mode fixture`.
This rewrites `golden/ranked_order.json` in place.
4. **Review the diff like a code review, never auto-accept it.** `git diff evals/golden/ranked_order.json` and confirm every path that moved makes sense.
If you cannot explain a change, do not commit it -- investigate the pipeline instead.
5. If the change was NOT intentional, do not regenerate: investigate `memento/search.py` (or, for the deep pipeline, `memento/retrieval_policy.py`) for the regression.

SQLite/FTS5 version changes are a known regen trigger: if a golden fails right after an environment upgrade (new Python, new OS image, new SQLite), suspect the scorer version before the ranking code -- and still review the regen diff.

### Adding golden queries (do this ~2 per month)

When you notice a real recall miss during a session (you knew a note existed but recall did not surface it), add it:

1. Find the note path in the vault.
2. Add an entry to the `positive` list: an `id`, the query phrased as a natural prompt (the way a user would type it, not keyword soup), and `expect_any` with a distinctive path substring.
3. Run `python3 evals/run_evals.py --suite retrieval_accuracy` to confirm the entry is well-formed.

Negative queries should describe topics genuinely absent from the vault.
If a negative query starts leaking because the vault legitimately gained that topic, replace the query, do not delete it.

### Changing thresholds

All bounds live in `thresholds.yml` with comments.
Rules:

- Tighten only after two consecutive weeks of passing at the tighter bound.
- Never loosen to silence a failure; that hides regressions permanently.
- Record every change in `docs/eval-baselines.md` with a one-line reason.

### Promoting a known-gap check

When a known-gap check reports FIXED (someone implemented the behavior):

1. In `evals/retrieval_probe.py` or `evals/suites/capture_e2e.py`, find the check and remove its `known_gap=True` flag (probe) or move the case out of the known-gap branch (gate cases).
2. Run the suite; the check must pass as a core check.
3. Note the promotion in `docs/eval-baselines.md`.

### Adding a new check

1. Pick the suite by subject: note content -> `suites/vault_content.py`, telemetry -> `suites/capture_health.py`, search behavior -> `evals/retrieval_probe.py` (hermetic) or `golden/retrieval_queries.json` (live), triage behavior -> `suites/capture_e2e.py`, whether a captured note becomes retrievable -> `evals/capture_retrieve_probe.py`.
2. Copy the shape of an existing `check(...)` call: id, plain-English title, value, direction, details with concrete examples, and a `remediation` string that tells a future reader exactly what to do.
3. Add warn/fail bounds to `thresholds.yml` with a comment.
4. Keep it deterministic and read-only.
If it needs an LLM, gate it behind `context["llm"]` and make the grading itself deterministic (string/schema checks, not judge opinions).

### Extending the fixture vault

Fixture notes live in `golden/fixtures/vault/` and support two placeholders: `{{DAYS_AGO_N}}` renders a date N days ago, and `{{ROOT}}` renders the temp root (used for project paths).
Give each new fixture note a unique nonsense token (like `zephyrcache`) so grep-backend queries are unambiguous.
If a new note changes what a `RANKED_ORDER_QUERIES` query in `retrieval_probe.py` matches, its golden top-5 will need regenerating (see above) -- run the fixture probe and check for `ranked_order_*` failures before committing.

### Things this suite deliberately does not do

- No LLM-as-judge: judge models drift and are not reproducible; every grading rule here is a string or schema check.
- No writes to the configured vault: safe to run from any session, including CI. `capture_retrieve_loop` writes notes, but only into its own throwaway temp vaults, never the vault `MEMENTO_VAULT_PATH`/config points at.
- No network calls (except the optional `--llm` extraction, which uses the locally configured backend).
- Latency/scale benchmarking stays in `benchmark/` (LongMemEval, optuna sweeps, scale lifecycle); this suite is about quality, not speed.

## Relationship to other tooling

- `memento-vault health` checks install/runtime wiring; this suite checks knowledge quality.
- `memento-vault retrieval-report` aggregates live retrieval telemetry (needs `retrieval_log: true`); this suite actively probes with known-answer queries.
- `benchmark/` measures performance and parameter tuning against external datasets; this suite measures YOUR vault and YOUR pipeline behavior.
