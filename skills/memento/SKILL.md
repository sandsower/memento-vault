---
name: memento
description: Capture the current session to the memento vault. Use when you want to record decisions, discoveries, or patterns from this session. Also use when the user says "remember this" or "save this to memento".
---

# Memento — manual session capture

Capture the current session as atomic Zettelkasten notes in the memento vault.

The vault location is configured in `memento.yml` (default: `~/memento`). Check `~/.config/memento-vault/memento.yml` or `~/memento/memento.yml` for the active config. If neither exists, use `~/memento`.

## When to use

- User invokes `/memento`
- User says "remember this", "save this", or similar
- A session contains noteworthy decisions or discoveries worth preserving

## Arguments

- `/memento` — capture the full current session
- `/memento "context"` — capture with the user's framing of what matters

## Process

1. **Scan the current session** for distinct ideas: decisions made, things discovered, patterns identified, bugs fixed, tools built or configured.

2. **Search existing notes** in the vault's `notes/` directory for related topics. Use Glob and Grep to check what's already there. Do not create duplicates — if a note already covers the same idea, update the Related section with a link instead.

3. **Create atomic notes** in the vault's `notes/` directory. One idea per file. Each note must have:

   ```yaml
   ---
   title: Short descriptive title
   type: decision | discovery | pattern | bugfix | tool
   tags: [relevant, tags, here]
   source: manual
   certainty: 1-5
   validity-context: what makes this true or false
   supersedes: "[[note-name]]" or omit
   project: /full/path/to/working/directory
   branch: branch-name-if-applicable
   date: YYYY-MM-DDTHH:MM
   session_id: current-session-id
   ---
   ```

   **Certainty scale:** 1 = speculative (untested idea), 2 = observed once (single session), 3 = confirmed in code (read it, verified), 4 = tested/shipped (PR merged), 5 = established pattern (seen across multiple tickets).

   **validity-context:** a short phrase describing what this note depends on. Examples: "while on feature branch X", "requires lib >= 2.0", "only in local dev". Omit if the note is unconditionally true.

   **supersedes:** if this note replaces an older one, link it. The older note stays in the vault but search should prefer the newer one.

   Body: the insight in 2-5 sentences. Context for why it matters. A `## Related` section at the bottom with `[[wikilinks]]` to related existing notes.

   File naming: slugified concept title. `redis-cache-requires-explicit-ttl.md`, not `2026-03-05-session.md`.

4. **Sanitize before writing.** Before writing any note to disk, scan the body and frontmatter for sensitive data and replace it with a generic descriptor. Strip:

   - **AWS account IDs** (12-digit numbers in AWS contexts) → `[AWS account]`
   - **API keys / tokens / secrets** (long alphanumeric strings, `sk-*`, `xoxb-*`, bearer tokens) → `[redacted]`
   - **Kennitala** (Icelandic national IDs, 10-digit `DDMMYY-XXXX`) → `[kennitala]`
   - **Email addresses** in note bodies (keep only if the email is the *subject* of the note, e.g., a contact flow design) → `[email]`
   - **Internal hostnames / IPs** (RFC 1918 addresses, `*.internal`, `*.local`) → `[internal-host]`
   - **IAM usernames / ARNs** → describe the role generically (e.g., "personal IAM user")
   - **Database connection strings** → `[connection-string]`

   Keep domain names that are public (e.g., `vicvalenzuela.com`, `fundid.is`) — those are fine. The goal is to prevent credential and identity leaks if the vault is ever shared, synced, or indexed. When in doubt, strip it — the session transcript has the original values if needed later.

5. **Update the project index** in the vault's `projects/` directory. Detect the project from the working directory and branch. Add `[[note-name]]` links under `## Notes` and a session line under `## Sessions`. Create the project index if it doesn't exist, using this template:

   ```yaml
   ---
   title: Project Name
   project: /full/path/to/working/directory
   branch: branch-name
   ---
   ```

   ```markdown
   ## Notes

   ## Sessions
   ```

6. **Run post-capture extensions.** Check if `~/.claude/skills/memento-post/SKILL.md` exists. If it does, read it and follow its instructions. This is the extension point for project-specific workflows (e.g., promoting notes to a team vault, tagging with domain-specific labels, notifying external systems). Skip this step if the file doesn't exist.

7. **Commit to vault repo.** After all writes are done, run:

   ```bash
   ~/.claude/hooks/vault-commit.sh "memento: [short description of what was captured]"
   ```

8. **Sync to remote vault.** If `~/.claude/memento-remote.env` exists, sync each note you created to the remote vault:

   ```bash
   (set -a; . ~/.claude/memento-remote.env; set +a; python3 ~/.claude/hooks/memento-remote-sync.py <note-paths>)
   ```

   Replace `<note-paths>` with the paths of notes you just created. The script is a no-op if `MEMENTO_VAULT_URL` is not set. Skip this step entirely if `~/.claude/memento-remote.env` does not exist.

9. **Trigger Inception check.** Manual captures bypass SessionEnd triage, so Inception's threshold check doesn't fire automatically. Run it explicitly:

   ```bash
   python3 ~/.claude/hooks/memento-inception.py --verbose 2>&1 | tail -5
   ```

   This is a no-op if Inception is disabled, if there aren't enough new notes, or if another instance is already running. It runs in the foreground but returns quickly (~20ms) if the threshold isn't met.

9. **Confirm to the user** what was captured: list the notes created and links added. Include any output from post-capture extensions.

## Rules

- Never delete or overwrite existing notes
- Never modify notes not created in this invocation
- Never write to `fleeting/`
- One idea per note — if you find yourself writing more than a paragraph, split it
- If the user provided context via `/memento "..."`, use their framing as the primary lens
- Use controlled tags — prefer reusing tags from existing notes over inventing new ones
