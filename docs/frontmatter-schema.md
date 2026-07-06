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
| `project` | string | Stable project slug (git repo toplevel basename, normalized via `repo_slug_from_path`); never a raw path or bare branch name (MEM-164) |
| `project_path` | string | Raw working-directory path the note was written from, preserved verbatim alongside the derived `project` slug (MEM-164) |
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

Callers pass `project` as either a raw cwd/path (the historical shape) or an already-derived slug; `write_note` splits path-like values into a stable `project` slug plus a separate `project_path` field carrying the original raw value verbatim (MEM-164). Tags are also normalized at write time: lowercased, trimmed, and spaces collapsed to dashes, with a config-driven `tag_aliases` map available to merge controlled-vocabulary synonyms.

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
project: my-api
project_path: /home/user/work/my-api
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
