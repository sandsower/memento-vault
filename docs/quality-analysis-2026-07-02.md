# Memento quality analysis - 2026-07-02

Follow-up to `docs/audit-2026-06-10.md`, focused on knowledge quality rather than architecture.
Every number below was measured today against the live vault (4,374 notes), the live triage-health telemetry (5,586 events), and the live QMD index (4,821 documents).
The new eval suite in `evals/` reproduces all of these measurements on demand: `python3 evals/run_evals.py`.

## TL;DR

Capture reliability was mostly fixed after the June audit (LLM failure rate is down from 52% to ~9%), but quality collapsed on two other axes:

1. **Input quality**: the vault is filling with transient run state.
19% of all notes are ephemeral (PR opened, verification status, handoff state), written as `type: discovery` with `certainty: 5`, which makes them decay-immune and permanently retrievable.
The 2026-07-01 pi backlog drain (1,473 triage spawns in one day, vs a median of ~10) alone added hundreds of these.
2. **Retrieval accuracy**: prompt recall is BM25-first, and BM25 fails on natural-language prompts.
On a 10-query golden set phrased the way users actually type, recall@5 is **0.20** through the real recall path, while semantic search would have rescued 5 of the 8 misses.
Worse, `recall_min_score` (0.6) is calibrated for BM25 scores (0.9+), so even when semantic results do participate, valid hits scoring 0.5-0.7 are filtered out.

The eval suite turns both axes, plus spend and pipeline health, into a graded scorecard so regressions are visible weekly instead of after a month of degraded sessions.

## Part 1: what is wrong today

### Input side (recording)

**Ephemeral notes at scale (FAIL, 19%).**
Sampled July notes are dominated by run-state: "MEM-20 PR handoff state", "RON-113 verification status before full gate completion".
These carry `certainty: 5`, so temporal decay never touches them, and they outrank real knowledge forever.
Root cause is twofold: the triage prompt never distinguishes durable knowledge from session state, and the `is_substantial` gate counts exchanges/files only, so long babysit/status sessions always pass.
The capture_e2e eval reproduces this deterministically: fed a status-only fixture transcript, the real prompt + model produced a "Release PR #42 Review Status" note.

**Project scoping is broken at write time (FAIL, 15% slugs).**
The `project` frontmatter field mixes absolute paths from two machines (`/home/vic/...`, `/Users/vicvalenzuela/...`), per-ticket worktree paths (`rondo-workspaces/RON-26` etc., each counted as its own project), bare slugs, and even the branch name `main` (67 notes).
Retrieval-side `filter_by_project` matches by path prefix, so cross-machine notes never match and worktree fragmentation splits one project into dozens.
Additionally, slug values are realpath'd against the process cwd, which can make a slug match any unrelated cwd (tracked as a known-gap eval check).

**Epistemic metadata is inconsistent.**
14% of notes have no certainty at all; a handful use a percent scale (95, 97) or 0.
22% use non-canonical types (595 `session`, 252 `project`, plus `checkpoint`, `permanent`, `observation`...), which the quality signals then penalize or ignore.
186 notes carry `supersedes`, but 8% of targets do not resolve, including literal artifacts like `supersedes: omit` where the model echoed the prompt's "optional" instruction.
Nothing validates the LLM's `supersedes` output against existing notes at write time.

**Linking has failed as a mechanism.**
59% of notes have zero outgoing wikilinks, and 10% of the links that do exist dangle.
The triage prompt shows the model only the first 100 note titles out of 4,374, so it cannot link meaningfully.
Graph-based retrieval features (PPR, wikilink expansion) are starved as a result.

**Capture volume is unbounded.**
June produced 2,255 notes (5x the March rate); the July 1 backlog drain wrote ~360 in two days.
Nothing rate-limits queued backlog processing, dedups near-identical notes across sessions (50 near-duplicate title groups exist, one with 7 copies), or caps notes per day.

### Output side (retrieval)

**The recall path cannot handle natural-language prompts (FAIL, recall@5 = 0.20).**
`qmd search` (BM25) answers keyword queries at 0.96-0.98 but returns empty or junk for question-phrased prompts, and recall queries are raw user prompts.
Semantic search (`qmd vsearch`) answers 6 of 10 golden queries, but recall only fuses it when the vector index is "warm", which on cold sessions means pure BM25.
And the score scales differ: semantic hits land at 0.5-0.7 while `recall_min_score` is 0.6, so calibrated-for-BM25 thresholds silently discard valid semantic results.

**Ranking barely uses the epistemic metadata the system works hard to capture.**
Certainty is only a decay-immunity flag (>=4) and a mild x0.9 penalty (<=2); a high-BM25 certainty-1 note outranks a certainty-5 note.
Undated notes never decay.
`supersedes` is not consulted at all: a superseded note ranks equal to its successor (known-gap eval check, currently OPEN).

**Telemetry cannot answer "was the injection useful".**
`retrieval_log` is off by default (6 events on this machine, ever), so `memento-vault retrieval-report` has nothing to aggregate.
The June audit's finding that tool-context went 0-for-64 was only discoverable by manual sampling.

### Cost side (model use and token spend)

Capture runs ~38 LLM calls/day (30-day window: 1,152 calls) through the claude CLI backend with sonnet.
Because CLI backends report bytes only, real token spend is invisible; the telemetry records prompt bytes (~100-400KB per call after truncation) but `input_tokens: null`.
The pi-bridge additionally logged 728 `status_failed` and 84 `tool-context_failed` events in June, i.e. 27 failures/day of pure waste from a broken host Python runtime.
Meanwhile the actually-valuable spend levers from the June audit (move triage to codex at $0, or anthropic-api with Haiku for ~$20-30/month with real token accounting) remain unimplemented.

## Part 2: recommended improvements (priority order)

1. **Teach triage to refuse run-state (input keystone).**
Add an explicit instruction block to `_build_structured_notes_prompt`: durable knowledge only; PR/ticket/verification status belongs in fleeting; when in doubt, write nothing.
Cap certainty for anything phrased as a status ("X is ready", "waiting for Y") at 2 with a short validity window.
Verify with `evals/run_evals.py --suite capture_e2e --llm`; the status fixture must produce zero notes.
2. **Fix recall for natural-language prompts (output keystone).**
Run semantic search unconditionally in recall (drop the warm-only gate) and fuse with RRF, or at minimum route question-shaped prompts to vsearch.
Recalibrate `recall_min_score` per result source instead of one global cutoff.
Verify with `retrieval_accuracy.golden_recall_at_5` (target: 0.7+ from today's 0.2).
3. **Normalize `project` to a stable slug at capture time.**
Derive a slug (git remote name, else basename with worktree suffixes stripped), backfill existing notes with a one-off migration, and make `filter_by_project` compare slugs, not realpath prefixes.
This also fixes the slug-matches-any-cwd bug.
4. **Rate-limit and dedup backlog processing.**
Cap queued pi captures per day, and route backlog notes through smart-store dedup so the 7-copy duplicate clusters cannot recur.
5. **Use the metadata in ranking.**
Make certainty a multiplicative ranking factor, decay (or floor-penalize) undated notes, and demote superseded notes below their successors.
The three known-gap eval checks flip to FIXED when done.
6. **Validate LLM output at write time.**
Reject or fix `supersedes` values that do not resolve to an existing note (and the literal `omit` artifact); reject non-canonical types instead of silently coercing to `discovery`.
7. **Turn on `retrieval_log` and make token spend real.**
Enable retrieval telemetry by default (it is capped and redacted already), and move triage extraction to a backend that reports token usage (anthropic-api Haiku, or codex for $0), so the `capture_health.spend_report` becomes a true cost dashboard.
8. **Wikilink generation needs real candidates.**
Give the extraction prompt the top-k related existing notes (by search on the draft note title) instead of the first 100 titles alphabetically.

## Part 3: the eval suite

Lives in `evals/`; full usage and maintenance guidance (written for less capable agents) in `evals/README.md`.
Baseline numbers recorded in `docs/eval-baselines.md`.

Design principles:

- One command (`python3 evals/run_evals.py`), one scorecard, exit code for automation.
- Deterministic by default: zero tokens, zero writes, zero network; runs in 1-2 minutes.
- LLM checks are opt-in (`--llm`), bounded (exactly 2 calls), and graded by deterministic rubrics, never by a judge model.
- Known gaps are tracked as informational checks so desired-but-unimplemented behavior stays visible without alarm fatigue.
- Every check carries a remediation line so any agent can act on a failure without re-deriving context.

Coverage map:

| Suite | Axis | Mechanism |
| --- | --- | --- |
| vault_content | input quality | scans all notes: schema, certainty, types, project hygiene, ephemerality, duplicates, links, sizes, supersedes, growth |
| capture_health | pipeline health + spend | grades triage-health telemetry: failure/truncation rates, yield, spawn storms, bridge failures, spend report |
| retrieval_accuracy | output quality | hermetic fixture vault through the real ranking functions, plus live golden/negative queries with BM25-vs-semantic contrast |
| capture_e2e | end-to-end capture | real triage gate on labeled sessions; with --llm, real prompt + model on fixture transcripts, rubric-graded |

Tests for the framework itself are in `tests/test_evals.py` (11 tests, all passing).
