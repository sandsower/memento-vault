---
name: memento-defrag
description: Review Memento Vault for stale low-value notes and archive confirmed candidates. Use for periodic vault maintenance.
---

# Memento Defrag

Archive stale notes without deleting knowledge.

## Process

1. Locate the vault from config or default to `~/memento`.
2. Read notes under `notes/` and parse frontmatter.
3. Identify archive candidates:
   - Certainty 1 or 2 and older than 60 days
   - Superseded by a newer note older than 14 days
   - Bugfix notes older than 90 days
   - Notes whose validity context is clearly obsolete
4. Never candidate notes that:
   - Have certainty 4 or 5
   - Are linked by 3 or more active notes
   - Were created in the last 30 days
5. Show candidates to the user and wait for confirmation.
6. Move confirmed candidates to `archive/`.
7. Update project indexes and active wikilinks where needed.
8. Reindex with `memento_reindex` if available, otherwise use QMD if installed.

## Rules

- Never delete notes.
- Never archive without confirmation.
- Never merge notes during defrag.
- Do not touch `fleeting/`.
