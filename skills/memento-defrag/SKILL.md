---
name: memento-defrag
description: Archive stale memento notes. Moves low-certainty, unaccessed notes to archive/. No merging, no deletion. Run periodically to keep the vault tight.
---

# Memento defrag — vault maintenance

Move stale notes to the vault's `archive/` directory to keep the active vault focused. No merging, no deletion. Git history preserves everything.

The vault location is configured in `memento.yml` (default: `~/memento`). Check `~/.config/memento-vault/memento.yml` or `~/memento/memento.yml` for the active config.

## When to use

- Periodically (monthly or when note count feels high)
- User invokes `/memento-defrag`
- Before a major project shift to clean up accumulated noise

## Process

1. **Read all notes** in the vault's `notes/` directory. Parse frontmatter for each file: `certainty`, `date`, `type`, `tags`, `validity-context`, `supersedes`.

2. **Identify archive candidates.** A note is a candidate if ANY of these are true:
   - `certainty` is 1 or 2 AND the note is older than 60 days
   - The note is superseded: another note's `supersedes` field references this note AND the superseding note is older than 14 days (grace period in case the new note is wrong)
   - `validity-context` references a dependency or version that has since changed (check `package.json` or similar if accessible)
   - `type: bugfix` AND older than 90 days (bugfix context decays fast)

   A note is NEVER a candidate if:
   - It has `certainty` 4 or 5 (tested/shipped or established pattern)
   - It is linked by 3+ other notes (it's a hub note)
   - It was created in the last 30 days regardless of certainty

3. **For notes missing `certainty`** (pre-metadata notes), infer from context:
   - Has `source: manual` -> treat as certainty 3
   - Has `type: decision` -> treat as certainty 3
   - Has `type: discovery` with `source: session` -> treat as certainty 2
   - Default -> certainty 2

4. **Show the candidate list** to the user before moving anything. Format:

   ```
   Archive candidates (X notes):

   - note-name.md — certainty 2, 75 days old, type: discovery
   - other-note.md — superseded by [[newer-note]]
   ...

   Notes staying active: Y
   ```

   Wait for user confirmation. The user can exclude specific notes.

5. **Move confirmed candidates** to the vault's `archive/` directory. Create the directory if it doesn't exist.

6. **Update wikilinks.** In any remaining active note that links to an archived note, keep the link but add `(archived)` suffix: `[[note-name]] (archived)`.

7. **Update project indexes.** In the vault's `projects/` directory, move archived note links from `## Notes` to a new `## Archived` section (create if needed).

8. **Commit to vault repo:**
   ```bash
   ~/.claude/hooks/vault-commit.sh "defrag: archived N notes"
   ```

9. **Reindex QMD** (if installed):
   ```bash
   qmd update -c memento && qmd embed
   ```

10. **Report** what was archived and what stayed.

## Rules

- Never delete notes. Move to archive/ only.
- Never merge notes. Each note stays atomic.
- Never archive without user confirmation.
- Never touch `fleeting/` or `projects/` content (only update links in projects).
- If QMD is not available, skip the reindex step.
- Notes in `archive/` are still searchable via grep and git history, just not in the active QMD index.
