---
name: memento
description: Capture durable knowledge from the current work session into Memento Vault. Use when the user asks to remember, save, capture, or record decisions, discoveries, bug fixes, or reusable patterns.
---

# Memento

Capture durable session knowledge as atomic notes in Memento Vault.

## Preferred Path

Use MCP tools when they are available:

1. Search first with `memento_search` to avoid duplicate notes.
2. Store each durable idea with `memento_store`.
3. For a broad end-of-session capture, use `memento_capture` with a concise structured summary.
4. Check vault health with `memento_status` if the vault location or connection is unclear.

## Fallback Path

If MCP tools are not available, use the configured local vault:

1. Read `~/.config/memento-vault/memento.yml` or `~/memento/memento.yml` to find `vault_path`.
2. Default to `~/memento` if no config exists.
3. Search `notes/` and `projects/` before writing.
4. Write new notes under `notes/`.
5. Update the relevant project index under `projects/`.
6. Commit vault changes if a commit helper or git repo is available.

## Note Shape

Each note should cover one idea and include YAML frontmatter:

```yaml
---
title: Short descriptive title
type: decision | discovery | pattern | bugfix | tool
tags: [relevant, tags]
source: manual
certainty: 1-5
validity-context: conditions where this remains true
project: /full/path/to/project
branch: branch-name-if-known
date: YYYY-MM-DDTHH:MM
session_id: session-id-if-known
---
```

Use certainty 1 for speculation, 2 for observed once, 3 for confirmed in code, 4 for tested or shipped, and 5 for established patterns.

## Sanitization

Before storing, redact credentials and sensitive identifiers:

- API keys, bearer tokens, OAuth tokens, SSH keys
- Cloud account IDs and IAM ARNs
- Database connection strings
- Private hostnames and RFC 1918 IP addresses
- Personal identifiers unless the note is explicitly about that identity

When unsure, redact.

## Output

Tell the user what was captured, what was skipped as duplicate, and where the notes were stored. Keep the response concise.
