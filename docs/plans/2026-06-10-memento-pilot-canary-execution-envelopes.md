# Memento Pilot/Canary Execution Envelopes (MEM-38)

Source plan: rondo repo, `elixir/docs/plans/2026-06-09-execution-envelope-structure.md`
(Phase 7) — a sibling Olin repository; this document stands alone if rondo is
not checked out.
Contract reference: `execution-envelope-v0` in `beislid/docs/configuration.md`.

This document defines the pilot/canary boundary for Memento automation hardening
work. It delivers one approved `execution-envelope-v0` per candidate ticket:

- **MEM-17** ([GH #94] Document automation MemoryProvider contract) —
  machine-readable copy: `docs/plans/envelopes/mem-17-memoryprovider-contract.envelope.yaml`
- **MEM-9** ([GH #97] Add automation memory health checks) —
  machine-readable copy: `docs/plans/envelopes/mem-9-automation-memory-health.envelope.yaml`

MEM-38 blocks MEM-17 and MEM-9; the approved envelopes below are what unblock
them. This document does **not** implement either ticket.

## Ownership boundary (applies to every envelope here)

Beislið owns work-contract, autonomy, and proof semantics. Rondo owns
execution, run evidence, and the run ledger. Memento owns curated memory and
learning — nothing else. No Memento envelope may turn the vault into a run
ledger, a proof store, or a coordination database. Run evidence produced while
carrying out an envelope belongs to Rondo; proof semantics (what counts as
"done" and how it is verified) belong to Beislið. Memento only keeps the
durable lessons.

## Canary criteria

These criteria are the bar for **any** future Memento AFK automation, including
the two envelopes below. An AFK run is a passing canary only if all of the
following hold:

1. **No silent failures.** Every gate, probe, or tool failure is surfaced in
   the delivery artifact. A run that swallows an error and reports success is a
   failed canary even if the diff is correct.
2. **Fail-closed where required.** Anything marked `required` (gates, proof
   requirements, dependency artifacts) blocks completion on failure or absence.
   Fail-open is permitted only where the contract explicitly says memory is
   optional — and even then the degradation must be reported, never hidden.
3. **Useful delivery artifacts.** Each run returns a summary, the changed
   files, gate/proof results, decisions made AFK with rationale, and open
   risks. "It's done" with no evidence is a failed canary.
4. **Bounded retries.** At most 3 attempts per failing gate or flaky step.
   After the bound, record the failure and either continue (if non-required) or
   pause (if required). Unbounded retry loops are a failed canary.
5. **Explicit pauses.** The run stops and returns `paused` with a precise
   reason when a pause condition fires: destructive or secret-bearing action
   needed, scope drift beyond the envelope, missing or contradicting dependency
   artifact, or required gates failing after bounded retries. Guessing past a
   pause condition is a failed canary.
6. **Memory-only Memento.** The run must not write run state, locks, proofs,
   queues, or coordination data into the vault. Lesson capture via the normal
   memento flows is the only allowed vault write outside the slice's own files.

## Envelope 1 — MEM-17 / GH #94: Document automation MemoryProvider contract

### Human-readable

Execute the approved MEM-17 docs slice AFK. You may write the MemoryProvider
contract documentation under `docs/` (a new `docs/automation-memory-provider.md`
or an agreed equivalent), covering: the pre-run context packet, explicit
search/get, note refs used by a run, post-run lesson capture, batch synthesis
from sanitized summaries, fail-open behavior when memory is optional, and
privacy/redaction expectations. The contract must explicitly prohibit raw proof
storage, active coordination state, locks, patches, full transcripts by
default, and Rondo-specific queues, and must include before/after examples of
Rondo/Beislið usage plus a description of the health/status signals that
indicate automation-memory availability. You may run the configured Beislið
gates and inspect diffs. Ask before adding new runtime code, changing MCP tool
behavior, or renaming existing public docs. Do not store run evidence, proofs,
or coordination state in the vault, and do not implement MEM-9's health checks
here — only describe the signals the contract expects. Pause if required gates
fail after 3 attempts, if the contract cannot be written without changing
runtime behavior, or if the ownership boundary would have to bend. Deliver the
new doc, gate results, decisions made AFK, and open risks.

### Machine-readable

```yaml
kind: execution-envelope-v0
status: approved
source:
  type: linear_issue
  id: MEM-38
  title: "[Envelope] Define pilot/canary execution envelopes for Memento automation"
  related:
    - type: linear_issue
      id: MEM-17
      title: "[GH #94] [P0] Document automation MemoryProvider contract"
    - type: github_issue
      repository: sandsower/memento-vault
      id: 94
      title: "[P0] Document automation MemoryProvider contract"
objective: >-
  Document the Memento automation MemoryProvider contract: allowed
  inputs/outputs, provider operations, failure behavior, privacy expectations,
  and explicit non-goals, so Rondo/Beislið can consume Memento as automation
  memory safely.
slice:
  id: mem-17-memoryprovider-contract-docs
  include:
    - New contract doc under docs/ (e.g. docs/automation-memory-provider.md)
    - >-
      Provider operations: pre-run context packet, explicit search/get, note
      refs used by a run, post-run lesson capture, batch synthesis from
      sanitized summaries
    - Fail-open behavior when memory is optional, and when fail-closed is
      required instead
    - Privacy/redaction expectations for everything entering the vault
    - >-
      Explicit prohibitions: raw proof storage, active coordination state,
      locks, patches, full transcripts by default, Rondo-specific queues
    - Before/after examples of Rondo/Beislið usage around a run
    - Description of health/status signals for automation-memory availability
      (signals only; implementation is MEM-9)
    - README/docs index cross-links as needed
  exclude:
    - Any runtime/MCP/CLI code changes
    - Implementing health checks (MEM-9 owns that)
    - A run ledger, proof store, lock, or coordination state in the vault
    - Rondo adapter internals or Beislið proof semantics
autonomy:
  allow:
    - Write and edit the contract documentation within the slice.
    - Add cross-links from existing docs/README to the new contract doc.
    - Run configured Beislið gates and inspect diffs.
  ask:
    - Add or change runtime code, MCP tool behavior, or CLI surface.
    - Rename or restructure existing public documentation.
    - Post to external trackers beyond the configured ticket-status updates.
  deny:
    - Store run evidence, proofs, locks, queues, or coordination state in the
      vault or describe them as supported.
    - Implement MEM-9 health checks inside this slice.
    - Weaken the prohibition list to make automation integration easier.
proof_requirements:
  - kind: proof-requirement-v1
    id: configured-pre-pr-gates
    type: command_gate
    stage: pre-pr
    status: required
    success_criteria:
      - "Configured Beislið gates in .beislid/workflow.md pass (docs-only
        changes still run ruff/format/compileall/test gates)."
    failure_policy:
      on_missing: block
      on_failure: block
      retryable: true
    expected_artifact:
      kind: gate_envelope
      reference: "gate transcript in the delivery output"
  - kind: proof-requirement-v1
    id: acceptance-checklist
    type: review_check
    stage: pre-pr
    status: required
    success_criteria:
      - "Docs describe Memento's role in automation."
      - "Contract includes allowed inputs/outputs and failure behavior."
      - "Health/status signals for automation-memory availability are described."
      - "Non-goals are explicit and hard to misinterpret."
    failure_policy:
      on_missing: block
      on_failure: block
      retryable: true
    expected_artifact:
      kind: checklist
      reference: "acceptance checklist in the delivery summary"
pause_conditions:
  - A required gate fails after 3 bounded attempts.
  - The contract cannot be documented without changing runtime behavior.
  - The ownership boundary (memory-only Memento) would have to bend.
  - A dependency artifact (execution-envelope-v0 contract, this envelope) is
    missing or contradicts the work.
dependencies:
  - GH sandsower/memento-vault#94 acceptance criteria (canonical scope).
  - execution-envelope-v0 contract in beislid/docs/configuration.md.
  - This approved envelope (MEM-38 deliverable).
  - .beislid/workflow.md gates available locally.
expected_delivery:
  summary: "What the contract defines, where it lives, and why."
  artifacts:
    - changed_files
    - gate_results
    - decisions_made_afk
    - open_risks
  next_step: "ready-for-review; human approves the contract doc before MEM-17 closes"
ownership:
  beislid: "Defines work-contract, autonomy, and proof semantics."
  rondo: "Owns execution and run evidence produced while carrying out this envelope."
  memento: "Owns curated memory and learning only; never a run ledger, proof store, or coordination database."
  teotl: "Deferred; no runtime/service responsibility."
```

## Envelope 2 — MEM-9 / GH #97: Add automation memory health checks

### Human-readable

Execute the approved MEM-9 slice AFK. You may add cheap, read-only health
reporting for automation memory: search backend availability, recent recall
failure rate, stale index warnings, local/remote divergence when configured,
last successful automation memory packet, and common failure reasons — exposed
as JSON suitable for provider probes (extending the existing `memento_status`
surface or an equivalent status path). Degraded or missing memory must be
visible but must not fail execution by default; failing closed is only for
callers that explicitly require memory. Secrets must never appear in any
output. Write tests first for the new health fields. You may run the configured
Beislið gates. Ask before adding network-dependent or slow checks to the
default path, adding new external dependencies, or changing the MCP tool list.
Do not write health history into the vault as notes, do not block default runs
on degraded memory, and do not turn the health surface into a run ledger. Pause
if required gates fail after 3 attempts, if cheap read-only reporting turns out
to require persistent state, or if secrets cannot be reliably excluded. Deliver
the changed files, tests, gate results, a sample health JSON, decisions made
AFK, and open risks.

### Machine-readable

```yaml
kind: execution-envelope-v0
status: approved
source:
  type: linear_issue
  id: MEM-38
  title: "[Envelope] Define pilot/canary execution envelopes for Memento automation"
  related:
    - type: linear_issue
      id: MEM-9
      title: "[GH #97] [P1] Add automation memory health checks"
    - type: github_issue
      repository: sandsower/memento-vault
      id: 97
      title: "[P1] Add automation memory health checks"
    - type: linear_issue
      id: MEM-17
      title: "[GH #94] [P0] Document automation MemoryProvider contract"
      note: "MEM-17's contract describes the signals this slice implements."
objective: >-
  Expose cheap, read-only, secret-free JSON health signals for automation
  memory so Rondo can see degraded or missing recall without runs failing by
  default.
slice:
  id: mem-9-automation-memory-health-checks
  include:
    - Health JSON exposing automation-memory readiness (search backend
      availability, recent recall failure rate, stale index warnings,
      local/remote divergence when configured, last successful automation
      memory packet, common failure reasons)
    - Read-only, cheap-by-default probe path suitable for Rondo provider probes
    - Extension of the existing status surface (memento_status / status CLI)
      rather than a new service
    - Tests for the new health fields, including the secret-free guarantee
    - Doc updates describing the health surface
  exclude:
    - Network-dependent or slow checks on the default path
    - Persisting health history, run state, or probe results in the vault
    - Failing runs by default when memory is degraded
    - New daemons, services, or background processes
autonomy:
  allow:
    - Implement read-only health reporting within the existing status surface.
    - Write tests first (TDD) for new health fields and secret exclusion.
    - Run configured Beislið gates and inspect diffs.
    - Update docs describing the health JSON.
  ask:
    - Add network-dependent or non-cheap checks to the default health path.
    - Add new external dependencies or change the MCP tool list.
    - Change behavior of existing status consumers in a breaking way.
  deny:
    - Include secrets, tokens, full note contents, or raw transcripts in any
      health output.
    - Block or fail execution by default on degraded/missing memory.
    - Store health/run/probe state in the vault as notes or coordination data.
proof_requirements:
  - kind: proof-requirement-v1
    id: configured-pre-pr-gates
    type: command_gate
    stage: pre-pr
    status: required
    success_criteria:
      - "Configured Beislið gates in .beislid/workflow.md pass, including the
        test gates covering the changed modules."
    failure_policy:
      on_missing: block
      on_failure: block
      retryable: true
    expected_artifact:
      kind: gate_envelope
      reference: "gate transcript in the delivery output"
  - kind: proof-requirement-v1
    id: acceptance-checklist
    type: review_check
    stage: pre-pr
    status: required
    success_criteria:
      - "Health JSON exposes automation-memory readiness."
      - "Rondo can consume readiness/probe metadata (sample JSON delivered)."
      - "Output remains cheap/read-only by default."
      - "Secrets are never included (covered by a test)."
    failure_policy:
      on_missing: block
      on_failure: block
      retryable: true
    expected_artifact:
      kind: checklist
      reference: "acceptance checklist plus sample health JSON in the delivery summary"
pause_conditions:
  - A required gate fails after 3 bounded attempts.
  - Cheap read-only reporting cannot be achieved without persistent state or a
    background process.
  - Secret exclusion cannot be reliably guaranteed in the output.
  - The work requires MEM-17 contract semantics that are missing or
    contradictory.
dependencies:
  - GH sandsower/memento-vault#97 acceptance criteria (canonical scope).
  - MEM-17 contract doc for the named health signals (soft dependency; signals
    are also enumerated in this envelope so MEM-9 can proceed in parallel).
  - execution-envelope-v0 contract in beislid/docs/configuration.md.
  - This approved envelope (MEM-38 deliverable).
  - .beislid/workflow.md gates available locally.
expected_delivery:
  summary: "Health surface added, fields exposed, default-path cost, and fail-open behavior."
  artifacts:
    - changed_files
    - test_results
    - gate_results
    - sample_health_json
    - decisions_made_afk
    - open_risks
  next_step: "ready-for-review; human reviews fail-open semantics before MEM-9 closes"
ownership:
  beislid: "Defines work-contract, autonomy, and proof semantics."
  rondo: "Owns execution, run evidence, and consumes the readiness probe."
  memento: "Owns curated memory and the health surface over it; never a run ledger, proof store, or coordination database."
  teotl: "Deferred; no runtime/service responsibility."
```

## Decisions made AFK

- **Combined the implementation plan and the envelope deliverable into this
  single doc.** MEM-38 is a docs/contract ticket; a separate plan file would
  duplicate this content. The pre-approved path for the deliverable is used.
- **Envelope status set to `approved`.** The MEM-38 pre-approved decisions call
  for "one approved envelope each"; MEM-38 itself is the approval act.
- **MEM-9's dependency on MEM-17 marked soft.** GH #97 only needs the signal
  list, which is enumerated in the MEM-9 envelope itself, so the tickets can
  run in parallel — matching the source plan's Phase 7 parallelism intent.
- **Machine-readable copies are standalone YAML files** under
  `docs/plans/envelopes/` so runners can consume them without parsing markdown;
  the YAML embedded here is kept identical.
- **CI/gate/schema/doc-drift hardening left out of scope.** The ticket lists it
  as "if needed"; no current drift requires a third envelope, and adding one
  would broaden scope without a grounding GH issue.

## Open risks

- The envelopes assume the existing `memento_status` surface is the right home
  for MEM-9's health JSON; if implementation finds it unsuitable, the `ask`
  lane covers the deviation.
- `proof-requirement-v1` field shapes follow the BEI-56/BEI-57 examples; if the
  Beislið contract evolves, these YAML files should be regenerated rather than
  hand-patched.
- The repo-wide `plans/` rule in `.gitignore` covers `docs/plans/`; these
  deliverables were force-added (`git add -f`). Any future file added under
  `docs/plans/` (e.g. a third envelope, or artifacts the MEM-17/MEM-9
  implementers produce here) will be silently ignored unless the author uses
  `git add -f` or a `!docs/plans/**` negation rule is added to `.gitignore`
  (deliberately left untouched in this slice).
