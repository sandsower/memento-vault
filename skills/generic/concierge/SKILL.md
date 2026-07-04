---
name: concierge
description: Search Memento Vault for day-to-day recall, past decisions, discoveries, implementation history, and prior session context. Use when the user asks "do you remember", "have we seen", "what did we decide", "what did we learn", "where was this implemented", "find prior context", "check memory", "search the vault", or any question that depends on prior work rather than current files alone.
---

# Concierge

Answer questions using Memento Vault history. This is the day-to-day retrieval workflow: use it before answering from memory when the user asks about prior decisions, discoveries, fixes, project history, recurring patterns, or whether something has been seen before.

## Trigger examples

Use this skill for prompts like:

- "do you remember how we handled this?"
- "have we seen this before?"
- "what did we decide about X?"
- "what did we learn from the previous session?"
- "where was X implemented?"
- "find prior context on X"
- "check memory / search the vault before answering"
- "what does memento know about X?"

## Search order

1. Use `memento_search` if available. It is the preferred explicit retrieval tool and returns backend/index metadata.
2. If MCP/tools are unavailable, use the local production CLI path, not ad-hoc grep first:
   - `memento-vault search "<query>" --limit 5 --detail-level summary`
   - or `python3 -m memento search "<query>" --limit 5 --detail-level summary`
   - For prompt-time context parity, use `memento-vault recall "<prompt>"` or `python3 -m memento recall "<prompt>"`.
3. If the local CLI is unavailable, read `~/.config/memento-vault/memento.yml` or `~/memento/memento.yml`, then use QMD directly if installed.
4. Use grep over `notes/`, `projects/`, and `fleeting/` only as a last-resort emergency fallback. Say that grep bypasses the production ranking/index path.
5. Use `memento_get` or local file reads when a specific note path needs full content.

## Response

Give the answer first, then cite the relevant note paths or project indexes. Mention the retrieval surface used (`memento_search`, local CLI, QMD, or grep) and call out when the result came from a degraded fallback or when local and remote results disagree.

## Rules

- Read-only only.
- Never invent vault contents.
- Prefer production search/recall paths over direct backend calls.
- If nothing relevant is found, say what was searched, the backend/fallback used, and suggest better search terms.
