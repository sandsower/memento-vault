# OpenCode integration

Memento works with [OpenCode](https://github.com/sst/opencode) over MCP. This guide wires the vault up end-to-end so an OpenCode session can search past notes, capture new ones, and leave fleeting activity markers automatically.

It assumes memento is already installed locally — either via `./install.sh` into `~/.claude/hooks/memento/` (the path Claude Code uses) or via `pip install memento-vault[mcp]` into a virtualenv.

## 1. MCP server config

Add a `memento` entry under `mcp` in `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "memento": {
      "type": "local",
      "command": ["python", "-P", "-m", "memento"],
      "enabled": true,
      "environment": {
        "PYTHONPATH": "/home/you/.claude/hooks",
        "MEMENTO_AGENT": "opencode"
      },
      "timeout": 10000
    }
  }
}
```

A few details worth knowing:

- `-P` strips the current working directory from `sys.path`, so the package always resolves through `PYTHONPATH` (or site-packages, if you `pip install`ed) regardless of where you launch `opencode` from. Without it you can accidentally import a sibling `memento/` directory from a checked-out fork — see `MEMENTO_OPENCODE_SESSION_ID` below for the related foot-gun.
- `MEMENTO_AGENT=opencode` tells memento's transcript adapter to use the OpenCode parser when a tool call passes `transcript_path=`.
- If you installed memento into a virtualenv with pip, point `command[0]` at that venv's Python (`/path/to/venv/bin/python`) and drop the `PYTHONPATH` entry — pip puts memento in site-packages.

Restart any running OpenCode TUI for the new MCP server to be picked up — OpenCode does not respawn MCP children inside a live session.

Verify with `opencode mcp list`. You should see `memento ✓ connected`.

## 2. AGENTS.md instructions

Drop the following into `~/.config/opencode/AGENTS.md` (or a project-local `AGENTS.md`) so the model proactively uses the vault:

```markdown
## Memento vault — persistent memory across harnesses

You have access to a memento vault via MCP tools (prefix `memento_`). The vault
holds atomic notes from prior sessions across every coding agent the user runs.

On the **first user message** of a session, call `memento_search` with the
working directory and a query derived from the user's request. Use the
returned notes as context before answering. If the user references "yesterday",
"last week", or "what we decided about X", search before answering — the model
must not rely on its own memory.

When the user says "remember this" / "save this" / "memento":

- Call `memento_store` for a single distinct fact.
- Call `memento_capture` at session end to triage the whole session.
```

Tune the prompt to your style. The key behaviors are: search at session start, store deliberately, capture at end.

## 3. Optional: auto-fleeting on session idle

OpenCode emits a `session.idle` event whenever a turn finishes. You can drop a small plugin that catches that event and writes a fleeting marker into the vault, even when the session was too small for a full triage capture. The marker is one line per session under `<vault>/fleeting/<YYYY-MM-DD>.md`.

Put this in `~/.config/opencode/plugins/memento-fleeting.ts`:

```typescript
import type { Plugin } from "@opencode-ai/plugin"
import { spawn } from "node:child_process"

const HELPER = `${process.env.HOME}/.local/share/memento-opencode/fleeting.py`

export const MementoFleetingPlugin: Plugin = async ({ client, directory }) => {
  return {
    event: async ({ event }) => {
      if (event?.type !== "session.idle") return

      const sessionID =
        (event as any)?.properties?.sessionID ??
        (event as any)?.id ??
        "unknown"

      const payload = JSON.stringify({
        session_id: sessionID,
        cwd: directory ?? process.cwd(),
        agent: "opencode",
      })

      // Detached spawn — the helper has to survive `opencode run` exiting
      // immediately after the assistant responds. Bun tears down stdio
      // handles on parent exit, so we unref the child and let it finish on
      // its own.
      const child = spawn("python3", [HELPER], {
        detached: true,
        stdio: ["pipe", "ignore", "ignore"],
        env: { ...process.env, PYTHONPATH: `${process.env.HOME}/.claude/hooks` },
      })
      child.stdin?.write(payload)
      child.stdin?.end()
      child.unref()

      client.app.log({
        service: "memento-fleeting",
        level: "info",
        message: `dispatched session=${sessionID} pid=${child.pid}`,
      })
    },
  }
}
```

And `~/.local/share/memento-opencode/fleeting.py`:

```python
#!/usr/bin/env python3
"""Append a session marker to memento's fleeting log.

Reads JSON from stdin: {session_id, cwd, agent}.
"""
import json
import sys

from memento.config import get_vault
from memento.store import (
    acquire_vault_write_lock,
    append_fleeting_session,
    release_vault_write_lock,
)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    try:
        vault = get_vault()
    except Exception:
        return 0

    if not acquire_vault_write_lock():
        return 0
    try:
        append_fleeting_session(
            vault,
            payload.get("session_id", "unknown"),
            cwd=payload.get("cwd"),
            agent=payload.get("agent", "opencode"),
        )
    finally:
        release_vault_write_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`append_fleeting_session` is the canonical writer — it handles per-day file creation, the dedup check on `session_id`, and the line format. Reuse it from any harness that wants to drop fleeting markers; don't reimplement the format.

## 4. Optional: full transcript capture

For substantive sessions, the model should call `memento_capture` directly with `transcript_path` pointing at OpenCode's SQLite session store, typically `~/.local/share/opencode/opencode.db`. Memento's OpenCode transcript adapter parses the `session`, `message`, and `part` rows out of that DB and feeds the same triage pipeline Claude Code uses.

You can scope the parse to a specific session id with the `MEMENTO_OPENCODE_SESSION_ID` environment variable — otherwise the most recently created session is used. Most callers leave it unset and rely on the "latest session" default; the env var exists for tooling that knows the session id (e.g. a future opencode plugin that captures synchronously).

## Troubleshooting

- **"not connected" mid-session**: OpenCode does not respawn MCP children that die. Restart the TUI; the next process spawns a fresh memento server. `opencode mcp list` from another shell will misleadingly report `✓ connected` because that command spawns its own transient server.
- **`memento_search` returns empty for notes you just wrote**: memento's index is updated by `memento_store` and friends. Direct file writes to `<vault>/notes/` bypass it. Run `memento_reindex` (via MCP) or capture through the official API.
- **Tool count looks low**: OpenCode silently drops some MCP tools depending on cwd. Until the upstream issue is resolved, launch `opencode` from a project directory with a `pyproject.toml` / `AGENTS.md` if you need the full tool surface.
