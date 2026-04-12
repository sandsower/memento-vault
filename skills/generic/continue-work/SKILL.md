---
name: continue-work
description: Recover recent working context from local state and Memento Vault. Use when the user asks to continue, resume, pick up where they left off, or find prior work.
---

# Continue Work

Recover context quickly, starting with local state and using the vault only when needed.

## Process

1. Inspect local state:
   - Current directory and git root
   - Current branch
   - Recent commits
   - Working tree changes
   - Nearby project memory files such as `MEMORY.md`, `AGENTS.md`, or `CLAUDE.md`

2. Summarize what is known:
   - Worktree
   - Branch
   - Recent work
   - Uncommitted changes
   - Pending items

3. Search Memento only when local context is thin or the user asks about prior history:
   - Prefer `memento_search` if available.
   - Fall back to QMD or grep over the configured vault.

4. Ask the user what to resume before making changes.

## Rules

- This skill is read-only.
- Do not modify files.
- Do not start implementation until the user chooses the next task.
- Prefer concrete file and note references over broad summaries.
