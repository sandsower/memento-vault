# Frontmatter Schema

Every atomic note in `notes/` has YAML frontmatter. Here's the schema.

## Required fields

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | Short descriptive title |
| `type` | enum | `decision`, `discovery`, `pattern`, `bugfix`, or `tool` |
| `tags` | list | Controlled tags for categorization |
| `date` | datetime | ISO 8601 with time: `YYYY-MM-DDTHH:MM` |

## Optional fields

| Field | Type | Description |
|-------|------|-------------|
| `source` | enum | `manual` (/memento), `session` (auto-captured), or `inception` (pattern consolidation) |
| `certainty` | int 1-5 | Epistemic confidence level |
| `validity-context` | string | What makes this note true or false |
| `supersedes` | wikilink | `[[older-note-name]]` if this replaces an older note |
| `synthesized_from` | list | Source note slugs (inception pattern notes only) |
| `project` | string | Full path to the working directory |
| `branch` | string | Git branch name |
| `session_id` | uuid | Claude Code session ID |

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
