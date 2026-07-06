# Frontmatter Schema

Durable notes in `notes/` use YAML frontmatter. This document is the human-facing schema for the fields emitted by current writers: Claude/session triage, MCP store/capture, Pi capture/curation, daily snapshots, and Inception pattern notes.

Run the drift checker whenever a writer or this document changes:

```bash
.venv/bin/python scripts/check_frontmatter_schema.py
# or, outside the repo venv:
python3 scripts/check_frontmatter_schema.py
```

CI runs the same checker so documented fields, note types, and source values stay aligned with implementation.

## Required fields

These fields are present on ordinary atomic notes written by `write_note` and its current callers.

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | Short descriptive title |
| `type` | enum | Canonical note type; see [Note types](#note-types) |
| `tags` | list | Controlled tags for categorization |
| `source` | enum-ish string | Capture family; see [Source values](#source-values) |
| `date` | datetime | ISO 8601 with time: `YYYY-MM-DDTHH:MM` |

## Optional fields

| Field | Type | Description |
|-------|------|-------------|
| `origin` | string | Specific adapter/tool path that wrote the note (for example `claude_triage:claude`, `pi_bridge:tool`, `mcp_capture:cursor`) |
| `certainty` | int 1-5 | Epistemic confidence level |
| `validity-context` | string | What makes this note true or false |
| `supersedes` | wikilink or title | `[[older-note-name]]` or note title if this replaces an older note |
| `synthesized_from` | list | Source note slugs (required on Inception pattern notes) |
| `project` | string | Full path to the working directory |
| `branch` | string | Git branch name |
| `session_id` | uuid/string | Agent session ID |

## Variant-specific fields

| Field | Type | Description |
|-------|------|-------------|
| `repo_slug` | string | Daily snapshot repository identifier; emitted by `write_daily_snapshot` |

Daily snapshots also accept caller-provided `frontmatter_extra` keys. The writer strips managed keys (`title`, `type`, `tags`, `source`, `certainty`, `date`, `repo_slug`, `supersedes`) before merging extras, so arbitrary extra keys are integration-specific rather than part of the managed schema.

## Source values

| Source | Current writer / meaning |
|--------|--------------------------|
| `session` | Local session triage (`hooks/memento-triage.py`) and default `write_note` source |
| `mcp` | `memento_store` low-level store primitive |
| `mcp-capture` | `memento_capture` session-summary note writer |
| `pi-capture` | Pi bridge manual/tool/lifecycle capture notes |
| `inception` | Inception pattern-note writer |
| `orra` | Daily snapshot writer for path-controlled integrations such as orra vault-bridge |
| `manual` | Legacy/manual local notes retained for compatibility; current Pi manual captures use `source: pi-capture` with an `origin` that identifies the manual/tool path |

## Compatibility

Older Pi bridge captures may have been written as `type: session` without `certainty`. New writes are normalized through the shared contract as typed notes (usually `type: discovery`, `certainty: 2`, `source: pi-capture`). Retrieval quality signals treat existing non-queued Pi `type: session` notes as low-certainty discoveries so they remain compatible without rewriting user vault history. The certainty backfill helper can also add `certainty: 2` to legacy session notes, `certainty: 3` to legacy manual notes, and `certainty: 3` to Inception pattern notes.

## Certainty scale

| Level | Label | Meaning |
|-------|-------|---------|
| 1 | speculative | Untested idea, hypothesis |
| 2 | observed | Seen once, needs validation |
| 3 | confirmed | Read the code, verified it's true |
| 4 | shipped | PR merged, tested in production |
| 5 | established | Seen across multiple tickets, reliable pattern |

Certainty is purely epistemic -- how sure this is true. It no longer confers
decay immunity on its own (MEM-150); see [Durability tier](#durability-tier)
for what does. Values outside 1-5 are clamped into range at write time with a
logged warning rather than rejected (`memento.store._coerce_certainty`);
`scripts/fix_certainty_values.py` is a one-shot fixer for notes written
before that guard existed.

## Durability tier

Retrieval decay immunity is driven by a tier derived from frontmatter, not
certainty: `memento.store.durability_tier(frontmatter, now)`. It is not a
managed frontmatter field written by any current writer -- it's computed at
read time by `apply_temporal_decay` and by the auto-archive sweep (below)
from the fields below.

- **`pinned`** -- optional bool frontmatter field, manually set
  (`pinned: true`). Permanent decay immunity.
- **`hot`** -- `last_resurfaced` (see [Compatibility](#compatibility)-adjacent
  resurfacing fields below) is within `durability_hot_window_days`
  (default 30) of now. Decay-immune.
- **`warm`** -- `resurfaced_count` > 0 at some point, but not within the hot
  window. Decays normally.
- **`cold`** -- never resurfaced. Decays normally. A certainty-5 note that
  has never been resurfaced decays exactly like a certainty-1 note (only
  certainty 3 gets a slower, not zero, decay rate).

`resurfaced_count` (int) and `last_resurfaced` (datetime) are folded into a
note's frontmatter from the access log by
`memento.store.fold_access_log_into_frontmatter` -- they are durable
retrieval-history fields, not something writers set directly.

### Auto-archive sweep (MEM-152)

`memento.archive.sweep_archive_candidates` is a scheduled sweep (triggered
from `hooks/memento-sweeper.py`'s periodic `main()`, alongside the MEM-148
fold) that reversibly archives `notes/*.md` files matching ALL of:

- `durability_tier` is `"cold"` (never resurfaced; `pinned`/`hot`/`warm` are
  never touched)
- `date` frontmatter age exceeds `archive_sweep_age_days` (config, default
  90)
- `certainty` is present and below 4

Notes missing a parseable `date` or `certainty` are skipped, not archived --
the sweep fails safe when a criterion can't be proven. Archiving moves the
file from `notes/` to `archive/` and appends a tombstone record via the same
ledger (`memento.archive.record_tombstone`) portable export/import already
use, so it is reversible (`memento.archive.restore_note`) and never a hard
delete. Gated by `archive_sweep_enabled` (config, default `false` -- a no-op
until enabled) and capped per run by `archive_sweep_max_per_run` (config,
default 50).

### Fleeting note lifecycle (MEM-153)

`memento.archive.fleeting_lifecycle_sweep` runs from the same
`hooks/memento-sweeper.py` periodic sweep, right after the MEM-152 archive
sweep above, and promotes or expires `fleeting/*.md` notes (see
`memento.store.append_fleeting_session` -- these are per-UTC-day session log
files and, as written today, carry no YAML frontmatter block of their own).

A fleeting note is promoted (moved to `notes/`, stamped with
`promoted_at: <ISO date>`, all other frontmatter preserved verbatim) when
EITHER:

- its `resurfaced_count` frontmatter (folded in by
  `memento.store.fold_access_log_into_frontmatter`, same as the durability
  tier above) is at least `fleeting_promote_min_resurfaced` (config, default
  2), or
- it is cited by a session-summary note: a `[[stem]]` wikilink to the
  fleeting note's filename stem appears in the body of any `notes/*.md` note
  whose `source` frontmatter is `mcp-capture`. There is no distinct
  `type: session-summary` frontmatter value in this schema -- `mcp-capture`
  is the literal, documented signal (see
  [Source values](#source-values) above: "`memento_capture` session-summary
  note writer") used to recognize a session-summary note. The check is a
  plain content scan, not the wikilink graph.

Anything left over whose age -- `date` frontmatter first, file mtime
otherwise -- exceeds `fleeting_expire_days` (config, default 14) is
reversibly archived via the same `archive_note`/`restore_note`/tombstone
machinery the MEM-152 sweep uses (`fleeting/<x>.md` archives to
`archive/fleeting/<x>.md`, never a second archive mechanism). A note with
neither a parseable `date` nor a readable mtime is skipped, never archived
on ambiguity. Gated by `fleeting_lifecycle_enabled` (config, default `false`
-- a no-op until enabled).

## Note types

| Type | When to use |
|------|-------------|
| `decision` | A choice was made between alternatives |
| `discovery` | Something learned or understood |
| `pattern` | A recurring approach or technique; Inception writes cross-note pattern notes with `source: inception` |
| `bugfix` | Root cause and fix for a bug |
| `tool` | A tool, script, or configuration created |
| `architecture` | System design, integration boundaries, or durable architectural context |
| `daily` | Structured daily snapshot written by `write_daily_snapshot` |

## Writer-specific shapes

### `write_note` / atomic captures

`write_note` normalizes canonical note types, tags, certainty, source, origin, validity context, supersedes, project, branch, and session id before writing. It always writes `title`, `type`, `tags`, `source`, and `date`; optional fields are written only when present.

### `write_daily_snapshot`

Daily snapshots are deterministic path-controlled notes named `notes/daily-<date>-<repo_slug>.md`. Their managed frontmatter is:

```yaml
---
title: Daily 2026-06-29 memento-vault
type: daily
tags: [daily, memento-vault]
source: orra
certainty: 2
date: 2026-06-29T14:30
repo_slug: memento-vault
---
```

When a snapshot supersedes an existing daily note, the writer adds `supersedes: "[[daily-<date>-<repo_slug>]]"`.

### Inception pattern notes

Inception writes `type: pattern`, `source: inception`, capped certainty, and a required `synthesized_from` list of source note slugs. It also adds `## Related` links back to source notes.

## Example

```yaml
---
title: Redis cache invalidation requires explicit TTL
type: discovery
tags: [redis, caching, backend]
source: session
origin: claude_triage:claude
certainty: 4
validity-context: while using Redis 7.x with cluster mode
project: /home/user/work/my-api
branch: feat/cache-layer
date: 2026-03-15T14:30
session_id: abc12345-def6-7890-ghij-klmnopqrstuv
---

Redis cluster mode does not propagate `DEL` commands across shards for keys
with no TTL set. Every cached key needs an explicit TTL even if you plan to
invalidate it manually, otherwise stale reads happen on replica shards.

Found this after 2 hours of debugging why the staging environment showed
old data after cache clear. The fix is setting a 24h TTL on all cache keys
as a safety net alongside explicit invalidation.

## Related

- [[redis-cluster-setup-notes]]
- [[caching-strategy-decision]]
```
