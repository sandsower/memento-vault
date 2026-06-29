# Frontmatter Schema

Every atomic note in `notes/` has YAML frontmatter. Here's the shared capture contract used by Claude triage, Pi capture/curation, and MCP write tools.

## Required fields

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | Short descriptive title |
| `type` | enum | `decision`, `discovery`, `pattern`, `bugfix`, `tool`, or `architecture` |
| `tags` | list | Controlled tags for categorization |
| `source` | enum-ish string | Capture family (`manual`, `session`, `mcp`, `mcp-capture`, `pi-capture`, `inception`, etc.) |
| `date` | datetime | ISO 8601 with time: `YYYY-MM-DDTHH:MM` |

## Optional fields

| Field | Type | Description |
|-------|------|-------------|
| `origin` | string | Specific adapter/tool path that wrote the note (for example `claude_triage:claude`, `pi_bridge:tool`, `mcp_capture:cursor`) |
| `certainty` | int 1-5 | Epistemic confidence level |
| `validity-context` | string | What makes this note true or false |
| `supersedes` | wikilink or title | `[[older-note-name]]` or note title if this replaces an older note |
| `synthesized_from` | list | Source note slugs (inception pattern notes only) |
| `project` | string | Full path to the working directory |
| `branch` | string | Git branch name |
| `session_id` | uuid/string | Agent session ID |

## Compatibility

Older Pi bridge captures may have been written as `type: session` without `certainty`. New writes are normalized through the shared contract as typed notes (usually `type: discovery`, `certainty: 2`, `source: pi-capture`). Retrieval quality signals treat existing non-queued Pi `type: session` notes as low-certainty discoveries so they remain compatible without rewriting user vault history. The certainty backfill helper can also add `certainty: 2` to legacy session notes.

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
| `pattern` | A recurring approach or technique |
| `bugfix` | Root cause and fix for a bug |
| `tool` | A tool, script, or configuration created |

## Example

```yaml
---
title: Redis cache invalidation requires explicit TTL
type: discovery
tags: [redis, caching, backend]
source: manual
origin: memento_skill
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
