# Automation MemoryProvider Contract

How automated runners (Rondo, Beislið, or any orchestrator) consume Memento as automation memory. This is a contract: the normative sections below use MUST / MUST NOT / SHOULD / MAY in the RFC 2119 sense, and downstream adapters are expected to hold to them.

If you remember one thing: **the vault is never a run ledger.** Memento owns curated memory and learning. Run state belongs elsewhere.

## Ownership boundary

| System | Owns |
|--------|------|
| Beislið | Work-contract, autonomy, and proof semantics |
| Rondo | Execution, run evidence, run ledger, coordination |
| Memento | Curated memory and learning — nothing else |

A runner that needs to record what happened during a run (gate results, proofs, retries, locks, queues) records it in its own ledger. Memento stores what was *learned*, after the run, in sanitized form.

## Contract at a glance

| Operation | Tool(s) | Direction | On failure |
|-----------|---------|-----------|------------|
| Pre-run context packet | `memento_session_context` (preferred); `memento_briefing`, `memento_recall`, `memento_tool_context` as host primitives | read | fail-open: `should_inject: false` / empty packet |
| Explicit search | `memento_search` | read | fail-open: miss envelope with structured reason |
| Explicit get | `memento_get` | read | fail-open: `{"error": ...}` dict, never an exception |
| Post-run lesson capture | `memento_capture` (session summary), `memento_store` (single atomic lesson) | write | error dict; runner proceeds, surfaces the failure |
| Batch synthesis | per-run `memento_capture` + Inception consolidation | write | best-effort background work |
| Availability check | `memento_status`, `memento-vault health` | read | safe partial dict; never raises, never prints secrets |

## Provider operations

### Pre-run context packet

Before a run starts, a runner SHOULD fetch one compact, budgeted context packet:

```json
memento_session_context({
  "cwd": "/path/to/repo",
  "prompt": "MEM-17: document the MemoryProvider contract",
  "session_id": "run-identifier-for-traceability",
  "token_budget": 2000
})
```

The packet combines a project briefing, prompt-relevant recall, and vault status in a single call, trimmed to `token_budget`. The `include_status`, `include_recent`, and `include_recall` flags (all default `true`) switch sections off when a runner wants less (`include_recent` gates the briefing section); `include_tool_context_preview` (default `false`) adds a tool-context preview section.

`memento_briefing`, `memento_recall`, and `memento_tool_context` are the underlying host-adapter primitives (session start, prompt time, and around file reads respectively). They return a `LifecycleResult` payload: `should_inject`, `content`, `source`, `results`, and optionally `reason` and `metadata`. Runners MAY call them individually; `memento_session_context` is preferred because it is budgeted and one round-trip. None of these are general user-answering search tools.

Runners SHOULD pass a stable `session_id` on every call in a run. It is a traceability marker inside note frontmatter and logs — it is not, and must not become, a run-ledger key.

### Explicit search and get

During a run, a runner MAY search the vault for prior decisions, fixes, and patterns:

```json
memento_search({
  "query": "how did we configure the release smoke gate",
  "cwd": "/path/to/repo",
  "limit": 5
})
```

On hits, results carry `path`, `title`, `score`, `snippet`, and (when readable) full `content`, so a follow-up `memento_get` round-trip is usually unnecessary. `memento_get` reads a known note by path or name.

A miss is data, not an error. On a miss, `memento_search` returns a structured envelope instead of raising:

```json
{
  "results": [],
  "miss": {
    "reason": "backend_unavailable",
    "recovery_hints": ["..."]
  }
}
```

On every miss, `results` (empty array), `miss.reason`, and `miss.recovery_hints` are present. `details` is the only conditional field — it appears only when there is something to report (for example `{"min_score": 0.7}` on `threshold_too_high`); adapters MUST NOT assume it exists.

Miss reasons include `no_exact_match`, `no_concrete_match`, `query_too_broad`, `empty_vault`, `backend_unavailable`, `project_filter_removed_all`, and `threshold_too_high`. Automation MUST treat a miss envelope as "no memory available", not as a run failure, and MUST NOT retry-loop on `empty_vault` or `backend_unavailable`.

### Note refs used by a run

Search results and packets expose note paths (`notes/<slug>.md`). A runner MAY record which note refs a run consumed — but that telemetry belongs in the **runner's own ledger** (Rondo's run evidence), never in the vault. Writing "run X used notes A, B, C" notes into the vault is run-ledger storage and is prohibited below.

### Post-run lesson capture

After a run, a runner SHOULD capture what was learned — as a sanitized summary, not a transcript:

```json
memento_capture({
  "session_summary": "MEM-17 docs run: contract doc landed; learned that docs-only changes still require the full gate suite.",
  "cwd": "/path/to/repo",
  "branch": "vic/mem-17-...",
  "files_edited": ["docs/automation-memory-provider.md"],
  "session_id": "run-identifier",
  "agent": "rondo"
})
```

`memento_capture` is the MCP equivalent of the SessionEnd hook. It writes a fleeting log entry and (for substantial sessions) an atomic note, and updates the project index. Captures are idempotent per `session_id`: a retried call returns `"deduplicated": true` instead of writing twice, so runners MAY safely retry on transport timeouts. `fleeting_only: true` records just the daily-log line for non-substantial runs.

`transcript_path` mode (full triage from a transcript file) is local/stdio-only; HTTP callers are rejected and must send `session_summary` instead. Even locally, automation SHOULD prefer summaries — see the transcript prohibition below.

For a single, well-formed lesson, `memento_store` writes one atomic note with typed frontmatter (`note_type`: discovery, decision, pattern, debugging, or architecture; `certainty` 1–5; optional `validity_context` and `supersedes`). The server does not validate `note_type` against that list — callers SHOULD stick to the five listed types so notes stay queryable. It is also idempotent: storing an identical payload returns the existing path with `"idempotent": true`.

Inputs to both MUST already be sanitized by the caller (see Privacy below). Both acquire a short-lived internal write lock; lock contention surfaces as an error dict whose message mentions the write lock, and such errors are retryable. (A structured `"reason": "lock_timeout"` field exists only on `memento_daily_snapshot`.)

### Batch synthesis from sanitized summaries

What exists today: each run captures its own sanitized summary via `memento_capture`, and Inception (the background consolidation agent) later clusters related notes across runs and writes pattern/synthesis notes. That combination — per-run capture plus asynchronous consolidation — is the supported batch-synthesis path.

A dedicated "submit N run summaries, synthesize now" endpoint does not exist and is not part of this contract. If one is added later, it will extend this document; runners MUST NOT emulate it by writing aggregate run-report notes into the vault.

## Failure behavior

**Fail-open is the default.** Memory is an optional input to a run:

- Degraded or missing memory MUST be visible to the caller (miss envelope, `should_inject: false`, empty packet, status flags) — and MUST NOT fail the run by default.
- A runner whose work genuinely requires memory MAY treat specific signals (for example `backend_unavailable`, or a stale `memento_status`) as fail-closed — but that is the caller's explicit decision, declared in its own work contract, not Memento's default.
- Silent failure is never acceptable: read operations return structured miss/status data rather than raising, and write failures return an error dict the runner MUST surface in its own run evidence.

## Privacy and redaction

Note bodies and summaries entering the vault pass `sanitize_secrets()`, which redacts API keys (`sk-…`, `sk-proj-…`), GitHub tokens (`ghp_…`, `gho_…`, `github_pat_…`), Slack tokens, AWS keys, JWTs, connection strings (`postgres://…` etc.), bearer tokens, and high-entropy `*_KEY/_SECRET/_TOKEN/_PASSWORD/_PASS` assignments. Tool output is additionally filtered for prompt-injection patterns, and health/status output never contains secrets.

That server-side pass is **defense-in-depth, not permission**:

- Callers MUST send sanitized summaries. Do not rely on the redaction patterns to catch a secret shape they don't know about.
- Callers MUST NOT send credentials, raw environment dumps, or unredacted logs as note bodies or summaries.
- Callers MUST NOT send full transcripts by default (see Prohibitions).

## Prohibitions

Automation consumers MUST NOT:

- **Store run evidence, proofs, or gate transcripts in the vault.** That is run-ledger material; it belongs in Rondo's run evidence store.
- **Use vault notes as locks, queues, or any active coordination state.** Coordination belongs to the runner's own infrastructure. (Memento's internal write lock is an implementation detail of safe file writes, not a coordination primitive offered to callers.)
- **Store patches or diffs as notes.** Patches belong in git branches and PRs; a note may *reference* a commit or PR.
- **Write full transcripts by default.** Capture sanitized summaries. Transcript-based triage exists for local interactive hosts, not as an automation default.
- **Create Rondo-specific queues (or any runner-specific work queues) in the vault.** No pending-work notes, no claim/ack markers, no scheduling state.
- **Weaken or work around this list to make integration easier.** If the integration seems to need one of these, the integration design is wrong — put that state in the runner's ledger.

Each prohibited item has a home; it just isn't here:

| Prohibited in the vault | Belongs in |
|------------------------|------------|
| Run evidence, proofs, gate results | Rondo run ledger / CI artifacts |
| Locks, queues, coordination state | Runner infrastructure |
| Patches, diffs | git branches / PRs |
| Full transcripts | Host transcript storage (local) |
| Work-contract / proof semantics | Beislið |

## A run, end to end

Before the run — fetch the context packet:

```json
memento_session_context({
  "cwd": "/repo", "prompt": "fix flaky retrieval test",
  "session_id": "run-7f3a", "token_budget": 2000
})
```

During the run — explicit recall when the runner hits something that smells familiar:

```json
memento_search({"query": "flaky test_deep_recall timeout fix", "cwd": "/repo"})
memento_get({"path": "notes/qmd-timeout-tuning.md"})
```

After the run — capture the lesson, sanitized:

```json
memento_capture({
  "session_summary": "Retrieval test flakiness was QMD cold-start, not the test. Warmed index in fixture; gate stable across 5 runs.",
  "cwd": "/repo", "branch": "fix/flaky-retrieval",
  "session_id": "run-7f3a", "agent": "rondo"
})
```

❌ **Prohibited** — this is a run-ledger entry wearing a note costume:

```json
memento_store({
  "title": "run-7f3a: gate results",
  "body": "ruff: pass, pytest: pass (412 tests), release-smoke: pass, proof hash 9c1f…"
})
```

Gate results and proof hashes go in the runner's ledger. The vault gets the *lesson* (cold-start caused the flake), not the *evidence*.

## Health and status signals

Available today, via `memento_status` (read-only, secret-free, cheap):

- `vault_exists`, `vault_path`, `vault_id` — is there a vault at all
- `qmd_available` — is the search backend present (when false, expect `backend_unavailable` misses; reads degrade, they don't break)
- `note_count`, `project_count`, `fleeting_count` — rough corpus size
- `config` — non-secret config summary (collection, backends, feature flags)

`memento-vault health` (CLI) runs deeper read-only checks — config parse, vault structure, backend availability, recent triage health from the 24-hour triage-health log, retrieval log health, lock files — with `--json` for structured output and `--strict` to exit non-zero on warnings. Its JSON includes an `automation_memory` readiness object for orchestration probes. `memento_status` exposes the same readiness object, and `memento_session_context` includes a compact probe summary in `sections.status.automation_memory`. Lifecycle packets also surface a triage-health warning inline when recent capture failure rates are high.

Automation memory readiness reports:

- search backend availability as explicit readiness metadata
- recent recall/search failure rate over a 24-hour window
- embedded-index staleness warnings when a local index exists
- local/remote divergence via the local sync ledger when `MEMENTO_VAULT_URL` is configured (no network probe by default)
- timestamp/shape of the last successful automation memory packet
- common failure reasons, structured from local health/retrieval/sync logs

All health surfaces are read-only, cheap by default, fail-open unless the caller explicitly chooses fail-closed, and MUST NOT contain secrets. A runner SHOULD check availability before a run if it intends to fail closed on missing memory; otherwise the fail-open defaults make a pre-check optional.

### Transport notes

The contract is transport-neutral: the same tools are exposed over stdio (local) and HTTP (remote). Remote callers should note two caveats: `memento_capture`'s `transcript_path` mode is rejected over HTTP (send `session_summary`), and write idempotency via `session_id` exists precisely so HTTP retries after timeouts are safe.

## Non-goals

To remove any doubt about scope, Memento as an automation MemoryProvider is **not**:

- a run ledger or execution history store
- a proof or evidence store
- a lock service, queue, or coordination database
- a transcript archive
- a Beislið work-contract or proof-semantics store

It is curated memory: lessons in, context packets out, sanitized everywhere.

## Related

- [How it works](how-it-works.md) — capture flow, retrieval (Tenet), Inception consolidation
- [Configuration](configuration.md) — health diagnostics, retrieval hooks, project rules
- Execution envelope for this contract: `docs/plans/envelopes/mem-17-memoryprovider-contract.envelope.yaml`
- Health-check implementation: MEM-9 (`docs/plans/envelopes/mem-9-automation-memory-health.envelope.yaml`)
