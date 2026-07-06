# Tool-context reassessment — 2026-06-28

Linear: MEM-59

## Decision

Keep tool-context injection, but treat it as a tightly gated diagnostic surface rather than a broad recall channel.

The implementation keeps automatic injection available for Claude/Pi host adapters, raises the default relevance bar to `tool_context_min_score: 0.75`, uses cwd-relative path keywords, requires a positive project match, preserves the existing session cap/dedup/rate limit, and adds retrieval-log decision events so future audits can measure behavior from logs instead of transcript guesswork.

## Why not remove it?

The previous audit found no demonstrable use in sampled transcripts, but the current code path had also accumulated fixes that materially change the risk profile:

- relative file paths now resolve against the session cwd before normalization;
- pre-fix cache entries are schema-invalidated;
- bridge/agent/config/vendor/system paths are skipped;
- queued/log-shaped junk notes are filtered by shared quality signals;
- tool context requires positive project metadata instead of accepting untagged notes as general knowledge.

Those gates make the feature low-volume enough to keep, provided it is observable. Removing it would discard a useful host-adapter primitive before collecting post-fix evidence.

## Current-behavior analysis

A local diagnostic probe against this checkout showed the important remaining behavior:

- `extensions/memento.ts` and top-level docs are skipped before search, as intended for bridge/config-like paths.
- searched code paths can still return `no-results` after positive-project filtering, which is safer than injecting unscoped notes.
- absolute checkout/worktree path terms polluted the BM25 query before this change (for example, `code rondo workspaces mem 59 memento lifecycle`). This could suppress relevant matches and made retrieval-log evidence harder to interpret.

The implementation now extracts keywords relative to the session cwd, so the same file becomes `memento lifecycle` instead of including local workspace/ticket scaffolding.

## Retrieval-log evidence added

When `retrieval_log: true` or `MEMENTO_DEBUG=1` is enabled, each `build_tool_context` call now emits one terminal event:

```json
{"hook":"tool-context","action":"decision","decision":"injected|no-results|cooldown|duplicate|..."}
```

Decision events include the file path, cwd, session id, lineage id when present, query, cache/search source, result counts, injected paths/titles/chars, latency, and skip-specific fields. Optional candidate summaries can be enabled with:

```yaml
tool_context_diagnostics_include_candidates: true
tool_context_diagnostics_max_candidates: 10
```

`tools/analyze-retrieval.py` now summarizes tool-context decision volume, injection rate, skip distribution, cache/search split, latency, and top injected paths.

## Follow-up audit guidance

After enough post-fix usage accumulates, run:

```bash
python tools/analyze-retrieval.py --since 7
```

Keep tool context only if logs show a non-trivial injected-path/use correlation in sampled sessions. If decision logs show mostly `no-results`, `duplicate`, or ignored injections, the next step should be default-off demotion or path-pointer-only output rather than broader injection.
