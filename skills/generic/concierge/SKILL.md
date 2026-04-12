---
name: concierge
description: Search Memento Vault for past decisions, discoveries, and session history. Use when the user asks what was decided, where something was implemented, or what prior sessions found.
---

# Concierge

Answer questions using Memento Vault history.

## Search Order

1. Use `memento_search` if available. It may include full note content in results.
2. If MCP is unavailable, use the configured local vault:
   - Read `~/.config/memento-vault/memento.yml` or `~/memento/memento.yml`.
   - Prefer QMD if installed.
   - Otherwise grep `notes/`, `projects/`, and `fleeting/`.
3. Use `memento_get` when a specific note path needs full content.

## Response

Give the answer first, then cite the relevant note paths or project indexes. Mention when a result appears remote-only or when local and remote results disagree.

## Rules

- Read-only only.
- Never invent vault contents.
- If nothing relevant is found, say what was searched and suggest better search terms.
