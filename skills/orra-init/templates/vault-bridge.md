## Vault Bridge

Optional integration between orra and [memento-vault](https://github.com/vicvalenzuela/memento-vault). When this directive is installed, orra's memory layer (daily notes, commitments, worktree context, recall queries) is routed through memento instead of the local `.orra/memory/` directory tree. Gives orra cross-repo memory; gives memento a structured daily-note format.

**Install only if you run memento-vault.** Without it, this directive no-ops on every call (memento MCP is unreachable) and the other directives fall back to their stock `.orra/memory/*` behavior.

### Activation Check

Before applying any routing, verify memento is reachable by calling `memento_status`. If the call errors or returns unhealthy, log a one-line notice: `vault-bridge: memento unreachable, stock directives will use .orra/memory/`. Defer to the stock directive's local paths for this invocation. Retry on next call (cheap).

### Routing Table

When another directive references a memory path or operation, apply the redirect below.

Structured daily content is written through the `memento_daily_snapshot` MCP tool, which owns the deterministic filename (`notes/daily-<date>-<repo-slug>.md`) and the append-only supersede chain. Commitments and worktree notes go to `projects/` where the vault guard allows direct edits. Everything else routes through the appropriate memento MCP tool.

| Stock reference | Memento redirect |
|---|---|
| Read `.orra/memory/daily/<date>.md` | `memento_get(path: "notes/daily-<date>-<repo-slug>.md")` |
| Write `.orra/memory/daily/<date>.md` | `memento_daily_snapshot(date: "<date>", repo_slug: "<repo-slug>", content: <filled-template>, frontmatter_extra: {project: <repo-root>, branch: <branch>, session_id: <sid>})` |
| Re-run shutdown same day | Retry `memento_daily_snapshot` with `supersede: true`. Tool writes `notes/daily-<date>-<repo-slug>-v<n>.md` with a supersedes link. |
| Recall search across daily notes | `memento_search(searches: [...], intent: "<recall phrasing>")` cross-repo; or `Glob <vault-root>/notes/daily-<date>-*.md` for deterministic date scoping |
| Read `.orra/memory/commitments.md` | `Read <vault-root>/projects/commitments.md` |
| Write `.orra/memory/commitments.md` | `Read` existing content, merge, `Write <vault-root>/projects/commitments.md`, then `~/.claude/hooks/vault-commit.sh` |
| Read `.orra/memory/worktrees/<id>.md` | `Read <vault-root>/projects/worktrees/<repo-slug>-<id>.md` |
| Write `.orra/memory/worktrees/<id>.md` | `Write <vault-root>/projects/worktrees/<repo-slug>-<id>.md`, then `vault-commit.sh` |
| Update `.orra/memory/index.md` | Skip. Memento's own index is authoritative. |

**Do NOT write to `<vault-root>/fleeting/*`.** Guarded and hook-only.

**Do NOT use `memento_capture` for shutdown content.** It's a session-ledger primitive, not a structured daily writer. It produces one-line fleeting entries plus optional auto-slugged atomic notes — the shutdown template is lost.

**Do NOT use `memento_store` for dailies.** It auto-generates the filename from the title; read-back needs the deterministic `daily-<date>-<repo-slug>.md` path that only `memento_daily_snapshot` produces.

**Do NOT `Write` directly to `notes/daily-*.md`.** The vault is append-only, and the tool handles the supersede chain for re-runs. A direct second `Write` either duplicates or gets blocked by the guard.

**Local `.orra/memory/` stays available as a live scratch area during the day.** The integration only writes to memento at structured moments (shutdown-ritual, morning-briefing's note creation). Daily edits that happen mid-session can still land in `.orra/memory/daily/<date>.md` as a working draft; the canonical archived version lands in `notes/daily-<date>-<repo-slug>.md` at shutdown via `memento_daily_snapshot`.

**Deriving `<vault-root>`:** prefer `~/Personal/memento/` for Vic's setup. For portability, a future install step may write the resolved path into `.orra/config.json`; until then, if the path doesn't resolve, log a warning and fall back to `.orra/memory/*` local writes rather than corrupting a different vault.

**Deriving `<repo-slug>`:** last path component of `git rev-parse --show-toplevel` (or `--git-common-dir`'s parent for bare-worktree layouts), lowercased, non-alphanumerics collapsed to `-`, then dots replaced with `_` (so `care.git/main` becomes `care_git`).

### Daily Note Convention

`shutdown-ritual` writes a structured template (today's focus, what shipped, still open, tomorrow's first move, loose ends, per-worktree state) to the daily note. Route that write through `memento_daily_snapshot(date, repo_slug, content, frontmatter_extra)`. The tool writes one file per repo per day at `notes/daily-<date>-<repo-slug>.md`, managed frontmatter, owned supersede chain.

Memento-triage's own `fleeting/<date>.md` (the cross-repo session ledger with a `## Sessions` header) stays untouched by vault-bridge. The per-repo daily note lives in `notes/`, not `fleeting/`:

- `fleeting/<date>.md`: memento-triage's session ledger, cross-repo, every session with ≥2 exchanges. Hook-only; vault-bridge does not touch it.
- `notes/daily-<date>-<repo-slug>.md`: orra's structured daily for this specific repo, written by shutdown-ritual once per repo per day via `memento_daily_snapshot`.

Per-repo filenames mean concurrent shutdowns from different repos never collide. A read of "yesterday's daily note" is an exact filename lookup in the current repo, not a search.

### Commitments Are Cross-Repo

`projects/commitments.md` is intentionally a single shared file, not per-repo. Linear tickets are user-global (the same assigned list comes back from any repo), and ad-hoc promises ("told @alice I'd have this by Thursday") are bound to a person or deadline, not a repo. Every `linear-deadline-tracker` run rewrites it idempotently with the same Linear content regardless of which repo the session is in.

### Cross-Repo Briefing Augment

After `morning-briefing` composes its repo-scoped picture, append a one-line cross-repo glance so the user is aware of other repos with recent activity without derailing the main briefing.

Procedure:

1. `Glob <vault-root>/notes/daily-<yesterday>-*.md` to list per-repo dailies from yesterday. Exclude the current repo's own file (`daily-<yesterday>-<repo-slug>.md`) and exclude any `-v<n>` variants (those are supersedes of the same repo's daily, not separate repos).
2. Extract the repo-slug suffix from each remaining filename (`daily-<yesterday>-<slug>.md` → `<slug>`). Sort by modification time, most recent first.
3. If the list is empty, skip the augment entirely. Silence is correct.
4. If non-empty, emit one line at the end of the briefing:
   `Yesterday you also touched: <comma-separated repo slugs>. Say "cross-repo recall" for a roll-up.`
   Cap the list at 5 repos; if more exist, show the 5 most recent and append `...and N more`.

Strictly additive: no changes to the core briefing composition, no interruption of today's focus.

### Cross-Repo Recall

When the user asks `cross-repo recall`, or a clearly cross-scoped question like "what did I ship yesterday across all repos" or "which repos have I been neglecting," produce a one-screen roll-up:

1. `Glob <vault-root>/notes/daily-<date>-*.md` for the target date (default: yesterday if phrasing is date-anchored; last 7 days for "this week" style questions). Prefer the unversioned `daily-<date>-<slug>.md` over any `-v<n>` variants for each repo — the latest supersede note is identical in shape, so read whichever is most recent.
2. For each file, `Read` it and extract the "Today's focus" and "What shipped" sections.
3. Group by repo slug. Per repo, show focus (one line) and shipped items (bulleted, max 3 per repo). Omit repos with no content.
4. End with a one-line summary: total repos touched, total items shipped, commitments that moved.

Prefer concise over comprehensive. The user asks for a cross-repo recall because they want a scan, not an archive dump. If they want a specific repo's detail, they can ask memory-recall with that repo in the query.

### Recall Queries

`memory-recall` asks questions about past work. When routed through vault-bridge:

1. Pick the right memento collection first: date-anchored questions hit `notes/daily-*` (via `Glob` or `memento_search`), decision/rationale questions hit `notes/` broadly, deadline questions hit `projects/commitments.md`.
2. Use `memento_search` with explicit `intent` describing the user's actual question, not a keyword paraphrase. Hybrid search quality degrades if the intent is vague.
3. Default to cross-repo scope. Only filter to the current repo if the user's phrasing is clearly scoped ("on fundid, what did I decide..."), otherwise return all matches and let the user narrow.

### Cross-Repo Scoping

Memento is cross-repo by default. For recall answers, include the originating repo in each cited result so the user can tell which context a decision came from.

Worktree notes are always repo-scoped by filename prefix (`projects/worktrees/<repo-slug>-<id>.md`) to prevent collisions when the same worktree id exists in multiple repos.

### Failure Modes

- **Memento MCP not registered:** exit routing silently, stock directives use local paths. This is the expected standalone-orra configuration.
- **`memento_daily_snapshot` returns `reason: already_exists`:** a daily for this (date, repo) already landed. Confirm intent with the user before retrying with `supersede: true`. If the user initiated a deliberate shutdown re-run, pass `supersede: true` directly.
- **`memento_daily_snapshot` returns `reason: invalid_date` or `reason: invalid_repo_slug`:** the caller's date format or repo-slug derivation is wrong. Fix the inputs before retrying — do not fall back to local writes, which would mask the bug.
- **`memento_daily_snapshot` returns `reason: write_failed` or `lock_timeout`:** log one-line warning, fall back to the local path for this write. Never drop memory on a write failure.
- **`memento_get` miss on read:** treat as "no note exists," identical to a missing local file. Stock directive's "memory is empty" on-ramp message still applies.
- **Slow memento response:** if a call exceeds ~5 seconds, log a notice and defer to local for this pass. Retry next call.

### What This Directive Is NOT

- Not a write-through cache. Once memento is live, `.orra/memory/` stops being written to. There is one source of truth: memento.
- Not bidirectional sync. Old `.orra/memory/*` files are not migrated automatically. If you have existing notes worth preserving, run a one-time import yourself.
- Not an orra requirement. Memento does not depend on orra; orra does not depend on memento. This directive is the only coupling surface, and it is opt-in.

### Dependencies

- `memento-vault` MCP server registered in Claude Code (`memento_status`, `memento_get`, `memento_store`, `memento_search`, `memento_daily_snapshot` tools available). `memento_daily_snapshot` was added after `memento_store` and is not present in older memento-vault releases — check with `memento_status` or probe the tool list before depending on it.
- Memento's `fleeting/`, `projects/`, `notes/` collections (memento defaults)
- No changes to orra code required. Routing lives in this directive file.

### Pairs With

- `morning-briefing`, `shutdown-ritual`, `memory-recall`, `linear-deadline-tracker`: all four have an anchor line at the top that defers to this directive's routing when installed. Without vault-bridge they run on `.orra/memory/*` as documented.

### Heartbeat

None. Vault-bridge is a passive routing directive, invoked only when another directive performs a memory operation.
