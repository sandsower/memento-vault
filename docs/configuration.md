# Configuration

Config file: `~/.config/memento-vault/memento.yml` (created by the installer).

## Config file locations (checked in order)

1. `~/memento/memento.yml` (vault root)
2. `~/.config/memento-vault/memento.yml`
3. `~/.memento-vault.yml` (home directory)

First file found wins. If none exist, defaults apply.

## Options

```yaml
# Where your vault lives
vault_path: ~/memento

# Sessions with more exchanges than this are "substantial"
# Substantial sessions spawn a background agent for atomic notes
exchange_threshold: 15

# Sessions editing more files than this are "substantial"
file_count_threshold: 3

# File patterns that force a session to be substantial
# (matched anywhere in the edited file path)
notable_patterns: [plan, design, MEMORY.md, CLAUDE.md, SKILL.md]

# QMD collection name (empty string disables QMD integration)
qmd_collection: memento

# Additional QMD collections to search
extra_qmd_collections: []

# Project rules (map directories to slugs and ticket patterns)
project_rules: []

# Auto-commit vault changes after triage
auto_commit: true

# Claude model for generating atomic notes
agent_model: sonnet

# Optional hardened mode for detached Claude workers.
# When true, memento/llm.py adds --bare to headless Claude calls.
# This skips hook/plugin/skill discovery and requires API-key or apiKeyHelper auth.
claude_bare_headless: false

# Seconds to wait before committing agent-written notes
agent_delay_seconds: 90

# --- Retrieval hooks ---
# Inject vault notes at session start
session_briefing: true
briefing_max_notes: 5
briefing_min_score: 0.55

# Inject vault notes before each prompt
prompt_recall: true
recall_min_score: 0.25         # normalized [0,1] relevance floor, see "Search backend and scoring" below
recall_max_notes: 3
recall_high_confidence: 0.55   # normalized score above this (single result only) skips the deep path
recall_skip_broad_project_queries: true  # Skip broad project/history prompts; use explicit search instead
recall_skip_patterns:
  - "^(yes|no|ok|sure|thanks|y|n|yep|nope|looks good|lgtm|ship it|continue)$"
  - "^git\\s"
  - "^run\\s"

# Inject vault notes on file reads
tool_context: true
tool_context_min_score: 0.75
tool_context_max_notes: 2
tool_context_max_injections: 5
tool_context_cooldown: 1       # seconds between QMD calls
tool_context_cache_ttl_hours: 24
tool_context_diagnostics: true
tool_context_diagnostics_include_candidates: false
tool_context_diagnostics_max_candidates: 10
```

## Health diagnostics

Run read-only operational checks with:

```bash
memento-vault health
memento-vault doctor   # alias
```

Default checks are cheap and local: config parse, vault directory structure, git/auto-commit readiness, selected search backend availability, automation-memory readiness metadata, install manifest state, managed hook/package file drift, Claude hook registration, MCP CLI registration, MCP config shape, optional Pi bridge config shape, stale headless Claude MCP config detection, recent triage health, basic retrieval log health, lock files, and basic Inception state when enabled. Drift checks are report-only and suggest installer repair commands such as `./install.sh --reinstall` or `./install.sh --mcp`. Use `--deep` for opt-in bounded live probes against configured integrations.

Options:

```bash
memento-vault health --json     # structured report
memento-vault health --verbose  # include sanitized details in human output
memento-vault health --strict   # exit nonzero on warnings
memento-vault health --deep     # opt-in live integration probes
```

Exit codes: failures always exit 1; warnings exit 0 unless `--strict` is set. The command never repairs state or prints secrets. `--deep` stays read-only but may contact configured integrations with bounded timeouts.

Legacy structural checker: `tools/vault-health-check.sh` is still supported for direct callers that need vault-content validation (`fleeting`/`notes`/`projects`/`archive` directories, note frontmatter, wikilinks, filename conventions, and git presence). It is intentionally not a replacement for `memento-vault health`; prefer the CLI health/doctor command for operational install/runtime diagnostics, and keep using the legacy script only for those low-level structural checks.

The JSON form includes an `automation_memory` readiness object with probe metadata for automated runners: search availability, recent recall/search failure rate, stale embedded-index hints, local sync-ledger divergence when a remote is configured, last successful automation-memory packet, and common failure reasons. It does not contact the remote vault by default.

Automated runners consuming the vault as memory should read the health/status signals section of the [automation MemoryProvider contract](automation-memory-provider.md) for what these surfaces guarantee (read-only, secret-free, fail-open by default).

## Project rules

Map working directories to project slugs and ticket patterns. Without rules, the slug is the directory name and tickets are extracted by a generic `[a-z]+-\d+` regex.

```yaml
project_rules:
  - path_contains: "my-company.git"
    slug: "my-company"
    ticket_pattern: "(PROJ-\\d+)"
  - path_contains: "side-project"
    slug: "side-project"
```

Checked in order. First match wins. `ticket_pattern` is optional.

## Extra QMD collections

Search additional QMD collections alongside the main vault. The concierge agent and the delta-check gate both use these.

```yaml
extra_qmd_collections: [team-knowledge, shared-docs]
```

Each collection must be configured in your `~/.config/qmd/index.yml`.

## Search backend and scoring

`search_backend: auto` (default) picks QMD -> embedded (SQLite FTS5 + sqlite-vec) -> grep, in that order, based on what's available. Every backend's `search()` result carries a `backend` field (`qmd`, `embedded-fts`, `embedded-vec`, or `grep`) alongside `path`, `title`, `score`, and `snippet`.

```yaml
search_backend: auto   # auto | qmd | embedded | grep
search_db_path: .search/search.db   # embedded backend's derived index, relative to vault_path

# Bounded-transform constant for the embedded backend's FTS5 BM25 score
# normalization: score / (score + fts5_score_k). Higher k compresses scores
# toward 0 (stricter); lower k lets weaker matches climb higher.
fts5_score_k: 2.0
```

Each backend normalizes its own relevance signal to `[0, 1]` at the `search()` boundary so a single `recall_min_score` / `recall_high_confidence` threshold behaves sensibly no matter which backend answers:

- **qmd**: QMD's own BM25/vector score, already ~bounded in practice (observed: BM25 hits 0.9-0.98, semantic hits 0.5-0.7) - only defensively clamped to `[0, 1]`, since we don't control the external binary's internal scale.
- **embedded-fts**: FTS5's raw BM25 rank is unbounded, so it's mapped via `score / (score + fts5_score_k)` - monotonic, bounded, and *not* a rescale relative to the current result batch (the old behavior forced the top hit in any batch to exactly 1.0, regardless of true relevance).
- **embedded-vec**: sqlite-vec's `notes_vec` table is declared with `distance_metric=cosine`, so cosine distance (`1 - cosine_similarity`, range `[0, 2]`) maps to `(cos_sim + 1) / 2` - identical direction -> 1.0, orthogonal/unrelated -> 0.5, opposite -> 0.0.
- **grep**: matched-terms / total-terms coverage fraction, already bounded by construction.

This normalization is a coarse, monotonic-per-backend signal, not a guarantee that the same score means the same thing on every backend - `recall_min_score` is a noise floor more than a fine-grained confidence signal (see `confidence_margin()` in `memento/retrieval_policy.py` for the relative rank-1-vs-rank-2 gap the deep pipeline actually uses to decide confidence).

## Post-capture extensions

The `/memento` skill checks for `~/.claude/skills/memento-post/SKILL.md` after creating notes. If the file exists, its instructions run as an extra step. Use this for things like promoting notes to a team vault or applying domain-specific tags.

## Tuning the triage

The triage decides which sessions get atomic notes vs fleeting one-liners.

**More aggressive capture** (capture more sessions):

```yaml
exchange_threshold: 8
file_count_threshold: 2
notable_patterns: [plan, design, MEMORY.md, CLAUDE.md, SKILL.md, test, spec, config]
```

**Less aggressive capture** (fewer notes, less noise):

```yaml
exchange_threshold: 25
file_count_threshold: 5
notable_patterns: [plan, design]
```

The delta-check gate (QMD-powered) prevents duplicate captures regardless of these thresholds. If QMD says the vault already covers a topic and no new files were edited, the agent is not spawned.

**LLM API backend controls** (used when `llm_backend` is `anthropic-api` or another API backend):

```yaml
llm_max_tokens: 4096
llm_api_retries: 3
llm_api_initial_backoff_seconds: 1.0
```

The Anthropic API backend retries 429/5xx responses and transient network errors with exponential backoff. Structured triage extraction also requests Anthropic tool-choice JSON when this backend is selected.

## Tenet — retrieval hooks

### Session briefing

At session start, `vault-briefing` injects a compact summary of your project's vault state into Claude's context. Includes recent sessions and the most relevant notes.

```yaml
# Disable the session briefing
session_briefing: false

# Show more/fewer notes
briefing_max_notes: 8

# Lower the threshold to surface more notes (default 0.3)
briefing_min_score: 0.2
```

Requires QMD. Falls back to project index notes if QMD is unavailable.

### Prompt recall

On every prompt, `vault-recall` runs a search and injects matching vault notes. It is semantic by default, with an opt-in literal path for identifier-shaped prompts. This is Tenet's just-in-time retrieval mechanism.

```yaml
# Disable prompt recall
prompt_recall: false

# Opt-in concrete/literal mode for path-like prompts.
# "auto" enables literal search only for prompts that look like paths,
# UUIDs, env vars, or quoted phrases; true forces literal search for every prompt.
# Default false preserves the existing conceptual recall behavior.
recall_concrete_mode: auto

# Tighter relevance threshold (fewer, more relevant results; default 0.25)
recall_min_score: 0.4

# Show more results per prompt
recall_max_notes: 5

# Custom skip patterns (prompts matching these are never searched)
recall_skip_patterns: ["^(yes|no|ok)$", "^git\\s", "^npm\\s"]
```

Keep this opt-in. Concrete mode is safer for exact identifiers and paths, but it bypasses the semantic/graph/rerank layers, so forced `true` can miss conceptual context.

Deduplication is automatic -- if the top result matches the last injection, it skips until 3 prompts have passed. Requires QMD.

### Tool context

When Claude reads a file, `vault-tool-context` extracts cwd-relative keywords from the file path and injects matching vault notes. Tool context is on by default in fresh installs. It is an unsolicited surface, so it is deliberately gated: it skips vendor/config/system/agent files, requires QMD, requires a positive project match, deduplicates against recall and prior tool-context injections, and uses a higher default BM25 threshold than prompt recall. Requires QMD.

```yaml
# Disable tool context
tool_context: false

# Tighter relevance threshold (default 0.75)
tool_context_min_score: 0.85

# More notes per file read (default 2)
tool_context_max_notes: 3

# Max total injections per session (default 5)
tool_context_max_injections: 8

# Rate limit between QMD calls in seconds (default 1)
tool_context_cooldown: 3

# Refresh directory-level cached results after N hours (default 24; 0 disables expiry)
tool_context_cache_ttl_hours: 24

# With retrieval_log: true or MEMENTO_DEBUG=1, log one terminal decision per call.
tool_context_diagnostics: true

# Include compact path/title/score candidate summaries in those decision logs.
tool_context_diagnostics_include_candidates: false
tool_context_diagnostics_max_candidates: 10
```

Use `memento-vault retrieval-report --since 7` (or `python tools/analyze-retrieval.py --since 7`) to summarize tool-context call volume, skip reasons, injection rate, injected paths, latency, cache/search split, top notes, and behavior recommendations from retrieval logs.

### Multi-hop retrieval (wikilink-following)

When the initial BM25 score is below the confidence threshold, `vault-recall` follows `[[wikilinks]]` from top results to pull in connected notes. It fetches the full content of the top 3 results, extracts wikilink targets, and retrieves linked notes directly via `qmd get`. Only fires on the deep path (low BM25 confidence).

```yaml
# Enable multi-hop (default false)
multi_hop_enabled: true

# Maximum linked notes to add per recall (default 2)
multi_hop_max: 2
```

Uses direct note lookups (not re-search), so overhead is 1-3 `qmd get` calls. 98% of vault notes have wikilinks, and 80% of recalls have followable links in their result set.

### Deep recall (experimental)

When confidence is low, spawns a background codex process for deeper analysis. Results are available by the next prompt.

```yaml
deep_recall_enabled: false
deep_recall_backend: codex     # "codex" or "claude"
```

### Tier 1 retrieval enhancements (v1.2.0)

These features improve recall quality with zero per-query LLM cost. All default to enabled and degrade gracefully if dependencies are missing.

```yaml
# PRF query expansion (two-pass BM25 with term extraction)
prf_enabled: true
prf_max_terms: 5       # max expansion terms extracted from initial results
prf_top_docs: 3        # initial results used for term extraction

# RRF hybrid search (fuses BM25 + vsearch when warm)
rrf_enabled: true
rrf_k: 60              # RRF constant (higher = more weight to top ranks)

# PageRank centrality boost
pagerank_alpha: 0.85          # PageRank damping factor
pagerank_boost_weight: 0.3    # score multiplier: score *= (1 + weight * pagerank)

# Access-log boost (derived runtime log; no note mutation)
access_log_enabled: true
access_log_boost_weight: 0.12
access_log_half_life_days: 30

# Personalized PageRank expansion (replaces 1-hop wikilinks)
ppr_enabled: true
ppr_max_expanded: 5    # max notes added via PPR
ppr_alpha: 0.85        # PPR damping factor
ppr_min_score: 0.01    # minimum PPR score to include

# Concept index (inception-produced keyword -> pattern note lookup)
concept_index_enabled: true
concept_index_score: 0.5      # score floor for concept index hits

# Project retrieval maps (instant project context from inception)
project_maps_enabled: true
```

PPR and PageRank require `networkx`. If not installed, recall falls back to 1-hop wikilink expansion (pre-v1.2.0 behavior). Concept index and project maps require Inception to have run at least once.

### Tier 2: Cross-encoder reranking

A local cross-encoder that rescores BM25/RRF candidates before injection. Runs on CPU via ONNX, no API calls.

```yaml
reranker_enabled: true
reranker_top_k: 10                                    # candidates to rerank
reranker_model: cross-encoder/ms-marco-MiniLM-L-6-v2  # ONNX model
reranker_min_score: 0.01                               # minimum reranker score
```

Only fires on the deep path (BM25 score below `recall_high_confidence`). Adds ~15-25ms for 10-20 candidates.

### Auto-archive sweep (MEM-152)

Periodically archives `notes/*.md` files that are durability-tier `cold`
(never resurfaced -- see [frontmatter-schema.md#durability-tier](frontmatter-schema.md#durability-tier)),
older than `archive_sweep_age_days`, and `certainty` below 4. Archiving moves
the file to `archive/` and records a reversible tombstone -- never a hard
delete. Runs from `hooks/memento-sweeper.py`'s periodic sweep.

```yaml
archive_sweep_enabled: false     # no-op until explicitly enabled
archive_sweep_age_days: 90       # note `date` frontmatter age threshold
archive_sweep_max_per_run: 50    # safety valve: max notes archived per run
```

### Fleeting note lifecycle (MEM-153)

Periodically promotes or expires `fleeting/*.md` notes so they stop
accumulating forever. A fleeting note is promoted to `notes/` (stamped with
`promoted_at`) when EITHER its `resurfaced_count` frontmatter is at least
`fleeting_promote_min_resurfaced`, or it is cited by a `[[stem]]` wikilink
from a `notes/*.md` note with `source: mcp-capture` (the documented
"session-summary note writer" -- see
[frontmatter-schema.md#source-values](frontmatter-schema.md#source-values)).
Anything left over that is older than `fleeting_expire_days` (`date`
frontmatter, falling back to file mtime) is reversibly archived the same way
the MEM-152 sweep above archives notes -- never a hard delete. Runs from
`hooks/memento-sweeper.py`'s periodic sweep, right after the MEM-152 archive
sweep.

```yaml
fleeting_lifecycle_enabled: false        # no-op until explicitly enabled
fleeting_promote_min_resurfaced: 2       # resurfaced_count threshold for promotion
fleeting_expire_days: 14                 # age threshold (date frontmatter, else mtime) for expiry
```

### Vault map & project hubs (MEM-160)

Replaces the old free-text `## Sessions`/`## Activity log` append (which
corrupted real `projects/<slug>.md` hubs into multi-hundred-line files with
duplicate headers and truncated entries) with mechanical, idempotent
regeneration. `memento.hub.regenerate_project_hub` rebuilds a project's hub
**from scratch** every time (frontmatter + the wikilink graph -- see
[how-it-works.md#project-hubs--vault-map-mem-160](how-it-works.md#project-hubs--vault-map-mem-160)
for the section schema), and `memento.hub.vault_map` assembles a capped
two-tier index (this project's hub plus top cross-project notes) for
briefing injection.

`hub_regeneration_enabled` gates the periodic sweep
(`memento.hub.regenerate_stale_hubs`, run from `hooks/memento-sweeper.py`
right after the MEM-153 fleeting lifecycle sweep) that regenerates hubs for
any project with notes newer than its hub file. `vault_map_in_briefing`
gates injecting `vault_map()`'s output into `memento.lifecycle.build_briefing`.
Both default to `false` -- flip them once you've reviewed a regenerated hub
and the assembled vault map.

```yaml
hub_regeneration_enabled: false   # no-op until explicitly enabled
hub_max_bytes: 25000              # ~25KB cap per regenerated projects/<slug>.md
vault_map_max_bytes: 25000        # ~25KB cap for the assembled two-tier vault map
vault_map_in_briefing: false      # inject vault_map() into the session-start briefing
```

## Disabling features

**No auto-commit** (commit manually):

```yaml
auto_commit: false
```

**No QMD** (grep-only search, disables retrieval hooks):

```yaml
qmd_collection: ""
```

**Disable retrieval hooks** (capture only, no briefing/recall/tool context):

These three toggles default to `true` in fresh installs.

```yaml
session_briefing: false
prompt_recall: false
tool_context: false
```

**Disable access-log boosts** (keep passive retrieval logging off or neutralized):

```yaml
access_log_enabled: false
```

**No background agent** (fleeting notes only):

```yaml
exchange_threshold: 9999
file_count_threshold: 9999
```
