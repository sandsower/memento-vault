---
name: start-fresh
description: Checkpoint useful session knowledge and pending work before starting a fresh context. Use when the user asks to start fresh, checkpoint, save and reset, or clear context.
---

# Start Fresh

Checkpoint the current session before the user starts a fresh context.

## Process

1. Capture durable knowledge with the `memento` skill or `memento_capture` MCP tool.
2. Detect the current work scope:
   - Prefer git root and branch.
   - If not in git, use the current directory name.
3. Identify pending work:
   - Unfinished implementation
   - Failing tests
   - Decisions still needed
   - Files or branches the next session should inspect
4. If the project has a memory file such as `MEMORY.md` or `AGENTS.md`, update only the pending-work section for the current scope.
5. Tell the user what was captured and what pending work was recorded.
6. Ask the user to start a fresh session or clear context using their current agent's normal workflow.

## Rules

- Do not write full session narratives into project memory files.
- Keep pending work short and actionable.
- Do not remove pending entries for other worktrees or branches.
- Do not clear context yourself unless the user explicitly asks and the environment provides a safe command.
