---
name: continue-work
description: Use when the user says "continue", "pick up where I left off", "what was I working on", or similar. Also use when starting a session and memory has pending work.
---

# Continue Work

Fast context recovery. Gather local state first, vault search only when needed.

## When to use

- User asks to continue, resume, or pick up where they left off
- User asks what was being worked on
- Starting a fresh session when memory has unfinished items

## Process

### Step 1 — Local context (do all in parallel)

Gather these simultaneously — no agents needed:

- **Scope detection**: Run `git rev-parse --show-toplevel` to get the current worktree/repo path. Extract the scope name (last path component). If not in a git repo, use the current directory name. This is your scope key for filtering MEMORY.md.
- **MEMORY.md**: Read the project's `MEMORY.md` from the auto memory path. Filter pending work to entries matching the current worktree only (look for `## Pending - <worktree>/` sections). Ignore pending entries from other worktrees.
- **Git state**: `git branch --show-current`, `git log --oneline -10`, `git status --short`. This tells you what branch, what was done recently, and what's uncommitted.
- **Active plan**: Check the plans directory for the most recently modified plan file. If one exists and references the current branch or ticket, read it.

### Step 2 — Present what you found

Give a brief summary:

- **Worktree:** current worktree path
- **Branch:** current branch name
- **Recent work:** 1-2 sentence summary from git log + any uncommitted changes
- **Pending items:** from MEMORY.md for this worktree only (if any)
- **Active plan:** link to plan file + current step (if any)
- **Suggested next step:** based on the above

### Step 3 — Ask, don't act

Ask the user what they want to pick up. Do NOT start working autonomously.

### Step 4 — Vault search (only if needed)

Skip the vault entirely unless:
- Local context is thin (no recent commits, no pending items, no plan)
- The user explicitly asks about history beyond the current branch

If vault search IS needed, spawn the concierge agent with a specific question — not a broad "find everything" query.

## Key principle

This skill is read-only. It never modifies files. It orients, then waits for direction.
