# MCP server

The MCP server exposes read, lifecycle, write, and maintenance tools over stdio (local) or HTTP (remote).
Any MCP-compatible agent can use them: Cursor, Windsurf, Codex, OpenCode, or Claude Code without native hooks.

## Tool inventory

<!-- memento-mcp-tools:start -->
The MCP server currently registers **20 tools**. This table is generated from `memento.mcp_inventory.MCP_TOOL_INVENTORY`; refresh/check it with `memento-vault tools --markdown` or `memento-vault tools --check`.

| Tool | Category | What it does | When to use it |
|------|----------|--------------|----------------|
| `memento_search` | Read | Search vault notes with BM25, optional semantic search, hybrid ranking, temporal decay, PageRank/access-log boosts, `concrete: auto\|true\|false` for exact identifiers and quoted phrases, and optional typed post-filters (type, tags, certainty, dates, branch, session_id, project) that mirror `memento_query`'s filter semantics. | Use before answering questions about past decisions, prior fixes, project history, session context, recurring patterns, or exact identifiers, including when ranked retrieval also needs metadata constraints. Do not use it to read a known note path. |
| `memento_query` | Read | Run typed metadata filters, counts, date buckets, and recent-session listings over note frontmatter without reading full note bodies. | Use for count/list/filter questions by project, type, tag, certainty, source, date, branch, or session_id; use `memento_search` instead for topical recall or semantic retrieval. |
| `memento_contradictions` | Read | Inspect a topic for disagreements, stale conclusions, supersession chains, and opposite-language hints. | Use when comparing competing notes about the same topic or when you need explicit superseded notes marked alongside their source paths and certainty/date context. |
| `memento_related` | Read | Walk the wikilink graph around a note: outbound/inbound links, a depth-limited neighborhood, and its supersession chain. Pure topology, no relevance scoring. | Use for "what links to X", neighborhood expansion, or finding the current/superseded version of a note; use `memento_search` instead for topical/semantic retrieval. |
| `memento_briefing` | Lifecycle | Build a compact session-start briefing payload for host adapters. | Host-adapter primitive for automatic injection; not a general user-answering tool. |
| `memento_recall` | Lifecycle | Build prompt-time recall context for host adapters. | Host-adapter primitive for automatic injection before an agent turn; not a general user-answering tool. |
| `memento_tool_context` | Lifecycle | Build read-tool context for a concrete file path. | Host-adapter primitive for automatic read-tool injection; not for explicit recall/search requests. |
| `memento_session_context` | Lifecycle | Build a one-call budgeted session context packet that can include status, recent context, recall, and tool-context preview metadata. | Host-adapter primitive that replaces separate briefing/recall/status calls when a host wants one compact payload. |
| `memento_get` | Read | Read full note content by name or path. | Use after `memento_search` when a returned path needs full content, or directly when the user already supplied an exact note path/name. |
| `memento_status` | Operational | Report vault health/status, note counts, project counts, and safe config summary. | Use for operational checks and setup debugging; not for recall, project history, or note content. |
| `memento_list` | Sync | List notes with optional content hashes for lightweight inventory/sync. | Use for sync/inventory clients; not for topical recall or reading notes. |
| `memento_store` | Write | Write a single knowledge note with managed frontmatter and project indexing. | Low-level write primitive for sync/automation or agents without skill support; interactive Claude/Codex sessions should prefer `/memento` or local hooks. |
| `memento_replace_note` | Sync | Replace an existing note at a known path for explicit conflict resolution. | Use only after a sync client has identified the exact remote path to resolve; it is not a general note creation primitive. |
| `memento_store_smart` | Write | Search for close matches before writing, and return duplicate/update/supersede suggestions. | Use when you want a write decision with candidate paths/reasons before creating a note; it avoids obvious duplicates by default. |
| `memento_daily_snapshot` | Write | Write a deterministic `notes/daily-<date>-<repo-slug>.md` snapshot with an append-only supersede chain. | Low-level structured daily-snapshot primitive for path-controlled integrations; do not use for ordinary notes, interactive memory capture, or session triage. |
| `memento_capture` | Write | End-of-session triage from a transcript path or structured summary; writes fleeting session state and optionally an atomic note. | Low-level SessionEnd equivalent for agents without hook support; not a replacement for interactive `/memento` workflows. |
| `memento_capture_run_lesson` | Write | Queue or explicitly write one typed automated-run lesson candidate with provenance refs. | Use for Rondo/Beislið-style post-run learning; queues by default and rejects raw logs, transcripts, run ledgers, proofs, and patch blobs. |
| `memento_synthesize_failures` | Write | Dry-run batch synthesis from sanitized external run summaries, with optional approved lesson-note writes. | Use for Rondo/Beislið-style batch failure learning; rejects raw logs/run stores and never executes advisory issue/gate/docs actions. |
| `memento_preserve` | Write | Archive a file or directory bundle under `archive/<slug>/` with a manifest, a lightweight index note, and optional project-linking. | Use for evidence packets, screenshots, handoff bundles, or other artifacts that should stay intact; copy by default, move only when explicitly requested. Do not use it for ordinary knowledge capture or atomic notes. |
| `memento_reindex` | Maintenance | Rebuild the search index from all markdown files after out-of-band changes. | Use after bulk adds, git pull, Obsidian sync, or stale-index evidence; not as the default response to a broad/empty search miss. |
<!-- memento-mcp-tools:end -->

The same table is kept in sync in the README.
Regenerate both from the single source of truth with `memento-vault tools --markdown`; never hand-edit the block between the markers.

## Automatic lifecycle context trust boundary

The four lifecycle tools return automatically injectable `content`, so they apply a stricter output contract than explicit search and get operations.
Every non-empty lifecycle `content` value is wrapped in the shared `MEMENTO_UNTRUSTED_DATA_V1` envelope before the MCP response leaves the server.
Persisted notes are treated as untrusted data regardless of source, origin, certainty, or citation state.
The encoded payload cannot create literal reminder tags, and the static reminder tells the consuming agent to use memory only as evidence rather than instructions.
This is defense in depth and must not be used to authorize tools, permissions, disclosure, or other side effects.

Host adapters should inject only the returned framed `content` field.
They must not remove the frame, append unframed note text, or truncate the rendered envelope.
`memento_session_context` reports separate units for its two limits.
`char_budget`, `used_chars`, and `raw_used_chars` measure raw retrieved content, while `framed_chars` measures the rendered trust envelope.
`packet_char_budget` and `serialized_chars` measure the complete serialized response.
The tool returns either a complete frame within both limits or no injectable content.

## Filtered search and graph lookups

Two tools added in the 2026-07-06 wave sharpen retrieval beyond plain ranked search:

- `memento_search` accepts optional typed post-filters (`type`, `tags`, `certainty_min`, `certainty_max`, `date_from`, `date_to`, `branch`, `session_id`, `project`) that mirror `memento_query`'s filter semantics.
  Filters run as a post-filter around the ranking pipeline: candidates are over-fetched, ranked as usual, then filtered by frontmatter metadata, so survivors keep their original rank order.
  Omitting every filter param leaves search behavior byte-for-byte unchanged.
- `memento_related` walks the wikilink graph around a note (outbound/inbound links, a depth-limited neighborhood, and its supersession chain) with pure topology, no relevance scoring.
  Use it for "what links to X" or to find the current version of a superseded note; use `memento_search` for topical/semantic retrieval instead.

## Common read paths

- Past decisions: `memento_search({"query": "What did we decide about cache invalidation?"})`.
- Prior fixes: `memento_search({"query": "Where did we fix stale headless Claude MCP config?"})`.
- Filtered search: `memento_search({"query": "cache invalidation", "type": "decision", "certainty_min": 4})`.
- Graph walk: `memento_related({"note": "redis-cache-ttl"})`.
- Exact identifier lookup: `memento_search({"query": "MEMENTO_VAULT_PATH", "concrete": "auto"})`.
- Disagreements/supersession: `memento_contradictions({"topic": "Redis cache"})`.
- Reading full content: take a returned `path` such as `notes/cache-policy.md`, then call `memento_get({"path": "notes/cache-policy.md"})`.

## Local retrieval without MCP

Hookless or tool-limited agents can still route through the same production search/recall policy without an MCP client:

```bash
memento-vault search "what did we decide about cache invalidation" --limit 5
memento-vault recall "how should we store bearer tokens that appear in URLs"
python3 -m memento search "MEMENTO_VAULT_PATH" --concrete auto
python3 -m memento reindex
```

`search` returns the explicit search envelope, including backend/index metadata.
`recall` runs the prompt-time recall path that host adapters use and is read-only unless `--record` is passed.

Run the MCP server manually:

```bash
python -m memento
```

`memento_capture` is the MCP equivalent of the SessionEnd hook.
Agents without hook support can call it at the end of a session with either a transcript path (local/stdio only) or a structured summary.

Consuming Memento from automated runners (Rondo, Beislið, or any orchestrator)?
The [automation MemoryProvider contract](automation-memory-provider.md) defines the allowed operations, fail-open behavior, privacy expectations, and explicit prohibitions -- in short: the vault is curated memory, never a run ledger.

## Connecting: local (stdio)

For Claude Code, register with the CLI:

```bash
claude mcp add memento-vault -s user -e PYTHONPATH="$HOME/.claude/hooks" \
  -- python3 -m memento
```

For Codex, register with the CLI:

```bash
codex mcp add memento-vault \
  --env PYTHONPATH="$HOME/.claude/hooks" \
  -- python3 -m memento
```

For other MCP-compatible agents (Cursor, Windsurf, OpenCode, etc.), add to your agent's MCP config:

```json
{
  "memento-vault": {
    "command": "python3",
    "args": ["-m", "memento"],
    "env": {"PYTHONPATH": "~/.claude/hooks"}
  }
}
```

## Connecting: remote (HTTP)

If you have a remote vault running (Docker, Fly.io, etc.), any MCP-compatible agent can connect over HTTP.
You need two things from whoever deployed the vault:

1. **Vault URL** (e.g. `https://vault.example.com`)
2. **API key** (a bearer token for authentication)

**Claude Code** -- register via the CLI:

```bash
claude mcp add -s user --transport http memento-vault https://vault.example.com/mcp \
  --header "Authorization: Bearer <your-api-key>"
```

**Codex** -- register via the CLI:

```bash
export MEMENTO_API_KEY=<your-api-key>
codex mcp add memento-vault \
  --url https://vault.example.com/mcp \
  --bearer-token-env-var MEMENTO_API_KEY
```

**Other MCP agents** (Cursor, Windsurf, OpenCode, etc.) -- add to your agent's MCP config file:

```json
{
  "memento-vault": {
    "type": "http",
    "url": "https://vault.example.com/mcp",
    "headers": {"Authorization": "Bearer <your-api-key>"}
  }
}
```

> **Note:** Claude Code ignores `~/.claude/mcp-servers.json`. You must use `claude mcp add` to register servers. Codex uses `codex mcp add`. The JSON config above is for other MCP clients only.

After connecting, the tools listed above are available.
Search returns full note content inline (no extra round-trip needed).
Restart your agent session after adding the config.

See [docs/remote-deployment.md](remote-deployment.md) for deploying the remote vault itself.
