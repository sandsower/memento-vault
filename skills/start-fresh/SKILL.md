---
name: start-fresh
description: Use when the user says "start fresh", "save and clear", "checkpoint", or similar. Also use proactively when context is getting large and compaction is approaching -- suggest this as a faster alternative.
---

# Start fresh

Capture the session to the memento vault, update MEMORY.md with pending work pointers, then prompt the user to `/clear`.

## When to use

- User asks to start fresh, save and clear, or checkpoint
- Context is getting large and you're about to compact — suggest this instead

## Process

1. Invoke the `/memento` skill to capture session knowledge to the vault (decisions, discoveries, patterns)
2. **Detect the current scope.** Run `git rev-parse --show-toplevel` to get the worktree/repo path. Extract the last path component as the scope name (e.g., `main` from `repo.git/main`, or `my-project` from `~/projects/my-app`). If not in a git repo, use the current directory name.
3. Review the conversation for any **pending work** — things that are unfinished or need follow-up
4. If there are pending items, update the project's `MEMORY.md`:
   - Use a heading that includes the worktree name: `## Pending - <worktree>/<topic>`
   - Add what's unfinished and pointers to relevant files/branches/plans
   - Remove any pending items from previous sessions that are now done **for this worktree only**
   - Do NOT touch pending entries for other worktrees
   - Do NOT write session summaries here — those belong in the vault
5. Tell the user: "Session captured. Run `/clear` to start fresh."

## What goes where

| Content | Destination |
|---------|-------------|
| Session narrative, decisions, discoveries | Memento vault (via `/memento`) |
| Pending/unfinished work pointers | MEMORY.md |
| User preferences, stable config | MEMORY.md (if not already there) |

## What to skip

- Session narrative in MEMORY.md (the vault handles that)
- Code snippets (the files themselves are the record)
- Anything already captured in a previous `/memento` call this session
