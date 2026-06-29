# Memento Vault

Persistent knowledge capture for coding agents. Sessions get triaged, scored, and filed as searchable Zettelkasten notes. Runs locally or as a remote service accessible from any device.

Works with Claude Code (native hooks), pi (native extension), and any MCP-compatible agent (Cursor, Windsurf, Codex, etc.) via the built-in MCP server.

## What it does

When a coding session ends, the triage pipeline reads the transcript and decides what to keep. Short sessions get a one-liner in a daily log. Substantial ones spawn a background agent that writes atomic notes with YAML frontmatter, wikilinks, and epistemic metadata (how confident is this note? what would make it wrong?). Everything lives in a local git repo you can browse with Obsidian or search with QMD.

For agents without native hook support, the MCP server exposes the same operations as tools: search the vault, store notes, capture sessions, read specific notes.

## Install

### Homebrew tap

```bash
brew tap sandsower/tap
brew install memento-vault
memento-vault install
```

### Git/manual install

```bash
git clone https://github.com/sandsower/memento-vault.git
cd memento-vault
./install.sh
```

Creates the vault at `~/memento`, copies hooks and the `memento/` package into `~/.claude/`, optionally sets up Obsidian views and QMD search. Works on Linux and macOS.

Custom vault path:

```bash
MEMENTO_VAULT_PATH=~/my-vault ./install.sh
```

The installer also links the `memento-vault` CLI into `~/.local/bin` when possible, so future updates can use `memento-vault update`. If `~/.local/bin` is not on your `PATH`, either add it or run the repository-local `./bin/memento-vault` directly.

To safely rerun setup for the same version without discarding local edits:

```bash
memento-vault install --reinstall
```

Check local vault/install health at any time:

```bash
memento-vault health         # concise read-only diagnostics
memento-vault doctor         # alias for health
memento-vault health --json  # structured output for automation
memento-vault health --deep  # opt-in live integration probes
```

Warnings exit 0 by default; failures exit 1. Use `--strict` if automation should fail on warnings too. The command is read-only: it reports suggested repairs such as `./install.sh --reinstall`, but does not modify vault/config files. JSON output includes `automation_memory` readiness metadata for runner probes without contacting remote services by default. Add `--deep` to run bounded live probes against configured integrations.

`--force` is reserved for recovery from broken installed files. It overwrites memento-managed files and requires confirmation, or `MEMENTO_FORCE=1` in non-interactive environments.

### Full install (hooks + retrieval + consolidation)

The base install captures knowledge and injects it back into active sessions. To also add background consolidation and the orra-init skill:

```bash
./install.sh --experimental
```

This adds extra modules:

- **Inception** -- background consolidation that clusters notes and synthesizes cross-session patterns
- **orra-init** -- experimental helper skill for starting new agents/workflows

Both require QMD. Inception also needs `pip install numpy hdbscan scikit-learn`. See [Tenet](#tenet) and [Inception](#inception) for details.

### MCP install (hookless agents)

For agents that support MCP but not native hooks (Cursor, Windsurf, etc.):

```bash
./install.sh --mcp
```

This installs the `memento/` package, writes generic MCP server config, and registers the server with Claude Code and Codex when those CLIs are installed. The server runs over stdio via `python -m memento`. The installer verifies the `mcp` Python package is available and installs it if needed. Claude Code gets Claude-specific skills and the concierge agent under `~/.claude`; Codex gets agent-agnostic skills under `~/.codex/skills`.

You can combine flags: `./install.sh --experimental --mcp` gives you the base hooks/context + MCP, plus Inception/orra-init extras.

### Pi extension

Memento ships a native pi extension from this repo. The extension is TypeScript, but lifecycle policy stays in Python core: pi calls a short-lived JSON adapter (`python3 -m memento.pi_bridge`) for briefing, recall, and read-tool context.

For local testing:

```bash
pi -e ./extensions/memento.ts
```

For package installation from a checkout:

```bash
pi install /path/to/memento-vault
```

The pi bridge does not start a long-lived MCP child process. Automatic durable writes are queued by default. Candidate captures land in local state for review and can be processed into curated notes manually.

Useful pi commands/tools:

- `/memento` — open the TUI dashboard for status, readable queued-capture review cards, explicit capture selection, processing previews, live group-level processing progress, and retry controls for failed processing groups.
- `/memento-status` or `memento_status` — bridge/vault status, lifecycle feature state, queue count.
- `/memento-queue` or `memento_queue` — list queued pi capture candidates with deterministic excerpts and size metadata.
- `/memento-process` or `memento_process` — process selected queued captures into curated durable notes. With no arguments the command shows a dry-run preview; use `/memento` for interactive selection and confirmation.
- `memento_capture` — manually write a durable note; pass `queue: true` to queue instead.

Pi bridge configuration can live in either `~/.config/memento-vault/pi-bridge.json`, project-local `.pi/settings.json`, or project `package.json`. The bridge reads `memento.piBridge` first, then `piBridge`, then top-level keys:

```json
{
  "memento": {
    "piBridge": {
      "enabled": true,
      "briefing": true,
      "promptRecall": true,
      "toolContext": true,
      "autoCapture": true,
      "captureQueue": true,
      "processQueue": true,
      "processQueueOnSessionClose": false,
      "processQueueMaxCaptures": 3,
      "processQueueModel": "claude-sonnet-4-20250514",
      "maxInjectedChars": 4000,
      "maxToolContextPerSession": 5
    }
  }
}
```

Environment variables override file config:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MEMENTO_PI_ENABLED` | `true` | Enable/disable the extension lifecycle work. |
| `MEMENTO_PI_BRIEFING` | `true` | First-turn project briefing. |
| `MEMENTO_PI_PROMPT_RECALL` | `true` | Prompt recall before each agent turn. |
| `MEMENTO_PI_TOOL_CONTEXT` | `true` | Read-tool context injection. |
| `MEMENTO_PI_MAX_INJECTED_CHARS` | `4000` | Per-injection character cap. |
| `MEMENTO_PI_MAX_TOOL_CONTEXT_PER_SESSION` | `5` | Tool-context injection cap per pi session. |
| `MEMENTO_PI_AUTO_CAPTURE` | `true` | Queue automatic capture candidates on `agent_end`, compaction, and shutdown lifecycle events. |
| `MEMENTO_PI_CAPTURE_QUEUE` | `true` | Queue automatic capture candidates instead of writing notes directly. |
| `MEMENTO_PI_PROCESS_QUEUE` | `true` | Enable manual queued-capture processing. |
| `MEMENTO_PI_PROCESS_QUEUE_ON_SESSION_CLOSE` | `false` | Reserved future automation route for processing a small batch on session close. |
| `MEMENTO_PI_PROCESS_QUEUE_MAX_CAPTURES` | `3` | Reserved future cap for session-close processing. |
| `MEMENTO_PI_PROCESS_QUEUE_MODEL` | `claude-sonnet-4-20250514` | Model for processor sessions; set config/env explicitly to override or `null` in config to use pi's default. |

When automatic capture is enabled (the default), pi lifecycle events only create reviewable queue entries in local state (`${MEMENTO_PI_STATE_HOME:-${XDG_STATE_HOME:-~/.local/state}/memento/pi}/queue/pi-captures.jsonl`). They do not write durable notes until `/memento-process` or `/memento` curates the queue into one or more atomic Memento notes. Processing runs write progress under `${MEMENTO_PI_STATE_HOME:-${XDG_STATE_HOME:-~/.local/state}/memento/pi}/processing/<run-id>/`, and the `/memento` footer shows a compact active/failed/interrupted indicator while background processing is visible. Failed processing groups keep their queue entries and can be retried directly from the TUI without reconstructing filters or selections. The processor prompt receives deterministic existing-note deduplication context and instructs curators to store original project/cwd/branch/session metadata as note frontmatter via `memento_capture`, not as prose boilerplate. Shutdown capture is skipped if another lifecycle capture was already queued during the same session.

Before cutting a pi bridge release, run this interactive smoke checklist from a checkout:

```bash
pi -e ./extensions/memento.ts
```

Then verify `/memento-status`, `/memento-queue`, `/reload`, `/new`, `/resume`, `/fork`, `/compact`, and quit. The bridge uses short-lived `python3 -m memento.pi_bridge` calls rather than a persistent child process, so shutdown cleanup should leave no memento-owned child process behind.

### Remote vault (access from any device)

Deploy the vault as a remote service so multiple devices and agents share the same knowledge base. The local install is the default; remote mode is opt-in.

**Connect to an existing remote vault:**

```bash
./install.sh --remote https://vault.example.com:8745
```

This installs hooks that sync to the remote vault over HTTP. A local vault is always created — remote mode is additive, not a replacement.

**Deploy the vault yourself** — four options depending on your setup:

| Option | Cost | What you need |
|--------|------|---------------|
| [Docker Compose](#docker-compose) | — | Docker on any machine |
| [Fly.io](#flyio) | ~$3-5/mo | Fly.io account |
| [Cloudflare Tunnel](#cloudflare-tunnel) | Free | Docker + Cloudflare account with a domain |

See [Cloud deployment](#cloud-deployment) for details.

### Upgrading from v1.x

The installer is version-aware. Modified hooks are preserved with `.new` copies for manual diffing. On subsequent upgrades, modified files are auto-merged via three-way merge (`git merge-file`). Existing opt-outs in your Claude/Pi config continue to win; rerun `./install.sh` to pick up the default hook set after upgrading.

```bash
cd memento-vault && git pull && ./install.sh
```

### Requirements

- Python 3.10+
- Git
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (for hook-based setup)
- [QMD](https://github.com/tobi/qmd) (optional, semantic search)
- [Obsidian](https://obsidian.md) (optional, browsing)
- `mcp` Python package (for MCP setup, installed automatically by `--mcp`)

## MCP server

The MCP server exposes read, lifecycle, write, and maintenance tools over stdio (local) or HTTP (remote). Any MCP-compatible agent can use them.

<!-- memento-mcp-tools:start -->
The MCP server currently registers **13 tools**. This table is generated from `memento.mcp_inventory.MCP_TOOL_INVENTORY`; refresh/check it with `memento-vault tools --markdown` or `memento-vault tools --check`.

| Tool | Category | What it does | When to use it |
|------|----------|--------------|----------------|
| `memento_search` | Read | Search vault notes with BM25, optional semantic search, hybrid ranking, temporal decay, PageRank boost, and `concrete: auto\|true\|false` for exact identifiers and quoted phrases. | Use before answering questions about past decisions, prior fixes, project history, session context, recurring patterns, or exact identifiers. Do not use it to read a known note path. |
| `memento_contradictions` | Read | Inspect a topic for disagreements, stale conclusions, supersession chains, and opposite-language hints. | Use when comparing competing notes about the same topic or when you need explicit superseded notes marked alongside their source paths and certainty/date context. |
| `memento_briefing` | Lifecycle | Build a compact session-start briefing payload for host adapters. | Host-adapter primitive for automatic injection; not a general user-answering tool. |
| `memento_recall` | Lifecycle | Build prompt-time recall context for host adapters. | Host-adapter primitive for automatic injection before an agent turn; not a general user-answering tool. |
| `memento_tool_context` | Lifecycle | Build read-tool context for a concrete file path. | Host-adapter primitive for automatic read-tool injection; not for explicit recall/search requests. |
| `memento_session_context` | Lifecycle | Build a one-call budgeted session context packet that can include status, recent context, recall, and tool-context preview metadata. | Host-adapter primitive that replaces separate briefing/recall/status calls when a host wants one compact payload. |
| `memento_get` | Read | Read full note content by name or path. | Use after `memento_search` when a returned path needs full content, or directly when the user already supplied an exact note path/name. |
| `memento_status` | Operational | Report vault health/status, note counts, project counts, and safe config summary. | Use for operational checks and setup debugging; not for recall, project history, or note content. |
| `memento_list` | Sync | List notes with optional content hashes for lightweight inventory/sync. | Use for sync/inventory clients; not for topical recall or reading notes. |
| `memento_store` | Write | Write a single knowledge note with managed frontmatter and project indexing. | Low-level write primitive for sync/automation or agents without skill support; interactive Claude/Codex sessions should prefer `/memento` or local hooks. |
| `memento_daily_snapshot` | Write | Write a deterministic `notes/daily-<date>-<repo-slug>.md` snapshot with an append-only supersede chain. | Low-level structured daily-snapshot primitive for path-controlled integrations; do not use for ordinary notes, interactive memory capture, or session triage. |
| `memento_capture` | Write | End-of-session triage from a transcript path or structured summary; writes fleeting session state and optionally an atomic note. | Low-level SessionEnd equivalent for agents without hook support; not a replacement for interactive `/memento` workflows. |
| `memento_reindex` | Maintenance | Rebuild the search index from all markdown files after out-of-band changes. | Use after bulk adds, git pull, Obsidian sync, or stale-index evidence; not as the default response to a broad/empty search miss. |
<!-- memento-mcp-tools:end -->

Common read paths:

- Past decisions: `memento_search({"query": "What did we decide about cache invalidation?"})`.
- Prior fixes: `memento_search({"query": "Where did we fix stale headless Claude MCP config?"})`.
- Project history: `memento_search({"query": "memento-vault recent triage failures project history"})`.
- Exact identifier lookup: `memento_search({"query": "MEMENTO_VAULT_PATH", "concrete": "auto"})`.
- Disagreements/supersession: `memento_contradictions({"topic": "Redis cache"})`.
- Reading full content: take a returned `path` such as `notes/cache-policy.md`, then call `memento_get({"path": "notes/cache-policy.md"})`.

Run manually:

```bash
python -m memento
```

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

For other MCP-compatible agents (Cursor, Windsurf, etc.), add to your agent's MCP config:

```json
{
  "memento-vault": {
    "command": "python3",
    "args": ["-m", "memento"],
    "env": {"PYTHONPATH": "~/.claude/hooks"}
  }
}
```

`memento_capture` is the MCP equivalent of the SessionEnd hook. Agents without hook support can call it at the end of a session with either a transcript path (local/stdio only) or a structured summary.

Consuming Memento from automated runners (Rondo, Beislið, or any orchestrator)? The [automation MemoryProvider contract](docs/automation-memory-provider.md) defines the allowed operations, fail-open behavior, privacy expectations, and explicit prohibitions — in short: the vault is curated memory, never a run ledger.

### Connecting to a remote vault via MCP

If you have a remote vault running (Docker, Fly.io, etc.), any MCP-compatible agent can connect over HTTP. You need two things from whoever deployed the vault:

1. **Vault URL** (e.g. `https://vault.example.com`)
2. **API key** (a bearer token for authentication)

**Claude Code** — register via the CLI:

```bash
claude mcp add -s user --transport http memento-vault https://vault.example.com/mcp \
  --header "Authorization: Bearer <your-api-key>"
```

**Codex** — register via the CLI:

```bash
export MEMENTO_API_KEY=<your-api-key>
codex mcp add memento-vault \
  --url https://vault.example.com/mcp \
  --bearer-token-env-var MEMENTO_API_KEY
```

**Other MCP agents** (Cursor, Windsurf, etc.) — add to your agent's MCP config file:

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

After connecting, the tools listed above are available. Search returns full note content inline (no extra round-trip needed). Restart your agent session after adding the config.

## Tenet

> Requires QMD.

Past knowledge flows back into active sessions via three hooks:

- **Session briefing** (SessionStart): injects your project's recent sessions and relevant vault notes when a session opens. Fast sync output (<50ms), QMD search deferred to background.
- **Prompt recall** (UserPromptSubmit): searches each prompt against the vault and surfaces matching notes before Claude processes it. Adaptive pipeline: fast BM25 path for confident matches, deep path (PRF, RRF, multi-hop wikilink-following, cross-encoder reranking) for low-confidence queries.
- **Tool context** (PreToolUse): injects vault notes when Claude reads files in known code areas. It uses cwd-relative path keywords, directory-level BM25 caching, rate limiting, a higher relevance threshold, and a positive project-match gate because file-read context is unsolicited.

All three hooks stay silent when they have nothing relevant. Zero tokens injected on trivial prompts, config files, agent/bridge files, and vendor directories. Enable `retrieval_log: true` or `MEMENTO_DEBUG=1` and run `tools/analyze-retrieval.py` to audit tool-context skip reasons, injection rate, injected paths, and latency.

### Performance

Benchmarked against 30 real sessions (381 prompts, 382 file reads, 16 projects):

| Metric | Value |
|---|---|
| Avg injected per session | ~597 chars (~149 input units) |
| Effective hit rate | 100% (when hooks search, they find relevant notes) |
| Avg recall latency | 472ms per prompt (adaptive pipeline) |
| Avg tool-context latency | 230ms per file read |
| Session briefing | <282ms (deferred QMD search is non-blocking) |
| LongMemEval NDCG@10 | 0.892 (retrieval quality, 500 questions) |

Full analysis in [docs/performance-analysis.md](docs/performance-analysis.md).

## Inception

> Requires QMD and `pip install numpy hdbscan scikit-learn`.

Inception clusters your vault notes by embedding similarity and synthesizes pattern notes -- higher-order insights that span multiple sessions. It runs as a detached background process after triage, or on demand via `/inception`.

```
Session ends -> triage completes -> inception check
  -> enough new notes? spawn background process
  -> HDBSCAN clusters ALL notes (not just new ones)
  -> only synthesizes clusters with new notes or refresh candidates
  -> writes pattern notes with source: inception
  -> backlinks source notes, commits, reindexes QMD
```

Opt-in via config:

```yaml
inception_enabled: true
inception_backend: codex    # "codex" (subscription, $0) or "claude" (API, ~$1/month)
inception_threshold: 5      # new notes before triggering
inception_max_clusters: 10  # max patterns per run
```

Pattern notes start at certainty 3 (subject to temporal decay and defrag). Use `/inception --dry-run` to preview clusters before writing. Full architecture and limitations in [docs/how-it-works.md](docs/how-it-works.md#inception-background-consolidation).

## What you get

```
~/memento/
  fleeting/       Daily logs, one line per session
  notes/          Atomic permanent notes (the good stuff)
  projects/       Project indexes linking notes and sessions
  archive/        Stale notes moved here by /memento-defrag
```

### Skills

| Command | What it does |
|---------|-------------|
| `/memento` | Capture insights mid-session |
| `/inception` | Find cross-session patterns, synthesize pattern notes (experimental) |
| `/memento-defrag` | Archive low-value notes, keep the vault focused |
| `/start-fresh` | Capture + save pending work + clear context |
| `/continue-work` | Recover context from local state and vault |

Agent-specific packaging:

- Claude Code skills live in `skills/` and are installed to `~/.claude/skills`.
- Claude Code agents live in `agents/` and are installed to `~/.claude/agents`.
- Agent-agnostic skills live in `skills/generic/` and are installed to `~/.codex/skills` when Codex is available. These are also the source to adapt for future Gemini, Kimi, or other MCP-capable agents.

### How triage works

```
Session ends -> triage hook fires (or memento_capture MCP tool)
  -> always: write fleeting one-liner (no LLM cost)
  -> if substantial: spawn background agent for atomic notes
  -> delta-check: skip if vault already covers the topic
  -> auto-commit + QMD reindex
```

"Substantial" means 15+ exchanges, 3+ files edited, or touching notable files (plans, designs, CLAUDE.md). All thresholds are configurable.

### Note format

Each note has YAML frontmatter: certainty score (1-5), optional validity context, type (decision/discovery/pattern/bugfix/tool), wikilinks to related notes. Full schema in [docs/frontmatter-schema.md](docs/frontmatter-schema.md).

## Cloud deployment

Deploy the vault as a service accessible from multiple devices, agents, or the claude.ai/code web interface.

### Docker Compose

The simplest option. Run on any machine with Docker — a home server, VPS, or your laptop.

```bash
MEMENTO_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))") \
  docker compose up -d
```

Vault is at `http://localhost:8745/mcp`. For TLS on a VPS, add Caddy or use `setup-remote.sh --host your-domain.com --tls`.

### Fly.io

Managed cloud with persistent volumes, automatic TLS, ~$3-5/mo.

```bash
fly launch --copy-config --no-deploy
fly volumes create vault_data --region iad --size 1 --yes
fly secrets set MEMENTO_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
fly deploy
```

Vault is at `https://<app-name>.fly.dev/mcp`. The included `fly.toml` is pre-configured.

### Cloudflare Tunnel

Expose a local Docker container to the internet via Cloudflare. No public IP needed, automatic TLS, free.

1. Create a tunnel in [Cloudflare Zero Trust](https://one.dash.cloudflare.com) → Networks → Tunnels
2. Set the tunnel's public hostname to point at `http://vault:8745`
3. Run:

```bash
export CLOUDFLARE_TUNNEL_TOKEN=<your-token>
export MEMENTO_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
docker compose -f docker-compose.cloudflare.yml up -d
```

### Connecting clients

Once the vault is running, connect any device:

```bash
MEMENTO_API_KEY=<key> ./install.sh --remote https://vault.example.com --experimental
```

The installer registers the remote MCP server with Claude Code and Codex when their CLIs are installed. Codex stores only the bearer-token environment variable name, so start Codex with `MEMENTO_API_KEY` available in the environment.

To upgrade an existing Claude-only remote install after installing Codex, rerun:

```bash
./install.sh --remote --experimental
```

If `~/.claude/memento-remote.env` exists, the installer reuses the saved remote URL and API key.

Or configure MCP directly:

```bash
# Claude Code
claude mcp add -s user --transport http memento-vault https://vault.example.com/mcp \
  --header "Authorization: Bearer <your-api-key>"

# Codex
export MEMENTO_API_KEY=<your-api-key>
codex mcp add memento-vault \
  --url https://vault.example.com/mcp \
  --bearer-token-env-var MEMENTO_API_KEY
```

For other MCP clients (Cursor, Windsurf, etc.), add to their MCP config:

```json
{
  "memento-vault": {
    "type": "http",
    "url": "https://vault.example.com/mcp",
    "headers": {"Authorization": "Bearer <your-api-key>"}
  }
}
```

## Architecture

The `memento/` package is agent-agnostic. Seven modules handle config, search, graph algorithms, vault I/O, LLM abstraction, and type definitions. Hooks and MCP tools are thin wrappers around this package.

```
memento/
  config.py          Configuration, project detection, vault identity
  search.py          Search pipeline: PRF, RRF, temporal decay, PageRank
  search_backend.py  Abstract search backend (QMD, Embedded, Grep — auto-detected)
  embedded_search.py Built-in search: SQLite FTS5 + sqlite-vec vectors, RRF hybrid
  embedding.py       Embedding providers: local nomic-embed-text, Voyage, OpenAI, Google
  indexer.py         Background indexer for files added outside the write path
  graph.py           Wikilink graph, PageRank, PPR expansion
  store.py           Vault I/O, write locking, dedup, note writing
  llm.py             5 backends: claude, codex, gemini, anthropic-api, openai-compat
  auth.py            Pluggable auth (bearer token, extensible to per-user)
  remote_client.py   HTTP client for hooks talking to a remote vault
  utils.py           Secret sanitization, tag normalization
  types.py           TypedDict definitions (SearchResult, NoteMetadata, SessionMeta)
  adapters/          Transcript parsing (Claude adapter, pluggable for others)
  mcp_server.py      MCP server (read/lifecycle/write/maintenance tools, stdio + HTTP transport)
```

LLM backend is configurable:

```yaml
llm_backend: claude        # claude, codex, gemini, anthropic-api, openai-compat
llm_model: sonnet          # model name for the chosen backend
claude_bare_headless: false # opt into Claude Code --bare for detached workers
```

`claude_bare_headless: true` is the hardened mode for headless Claude workers. It skips hook/plugin/skill discovery and requires API-key or `apiKeyHelper` auth.

## Configuration

Config lives at `~/.config/memento-vault/memento.yml`. All options in [docs/configuration.md](docs/configuration.md).

```yaml
vault_path: ~/memento
exchange_threshold: 15
file_count_threshold: 3
qmd_collection: memento
auto_commit: true

# Tenet retrieval hooks
session_briefing: true
prompt_recall: true
recall_concrete_mode: auto   # literal/path/UUID prompts only; true forces all prompts
tool_context: true
# When retrieval_log/MEMENTO_DEBUG is enabled, log tool-context decisions.
tool_context_diagnostics: true

# Retrieval pipeline
prf_enabled: true            # pseudo-relevance feedback query expansion
rrf_enabled: true            # reciprocal rank fusion (BM25 + vsearch)
ppr_enabled: true            # personalized pagerank link expansion
reranker_enabled: true       # cross-encoder reranking (local ONNX)
multi_hop_enabled: false     # follow wikilinks from top results
```

### Extension points

Three ways to layer project-specific behavior on top without forking:

- `project_rules` in config: map directories to project slugs and ticket patterns
- `extra_qmd_collections` in config: search additional QMD collections alongside the vault
- `~/.claude/skills/memento-post/SKILL.md`: post-capture hook that runs after `/memento` creates notes (e.g., promote to a team vault, apply domain tags)

## Search backends

The vault auto-detects the best available search backend:

1. **QMD** (if installed) — BM25 + vector search + reranking via external CLI tool (3.2GB)
2. **Embedded** (if onnxruntime + sqlite-vec installed) — built-in FTS5 + sqlite-vec with nomic-embed-text, RRF hybrid fusion. No external tools needed. Default on remote/Docker deployments.
3. **Grep** — substring matching fallback. Always works, no dependencies.

Override with `search_backend: qmd | embedded | grep` in config, or `MEMENTO_SEARCH_BACKEND` env var.

The embedded backend uses a single `search.db` SQLite file (derived, disposable). Markdown files stay the source of truth. Embeddings come from a local nomic-embed-text-v1.5 model by default (137MB, no API key). Optional API providers (Voyage, OpenAI, Google) configurable via `embedding_provider` in config.

### QMD (optional)

QMD adds semantic search over your vault. Without it the concierge agent uses the embedded backend or falls back to grep. QMD is required for Tenet and Inception.

```bash
qmd search "caching strategy" -c memento
```

The concierge agent uses QMD automatically when you ask about past decisions.

### Model warmup

Tenet's deferred briefing search uses vector search, which requires loading an embedding model. First call after a reboot takes 6-8s; subsequent calls are ~1.5s (model stays in OS page cache). The installer can add a background warmup to your shell rc file so the model is always cached:

```bash
# Added to .zshrc/.bashrc by the installer (optional)
[ -x /path/to/memento-vault/bin/memento-vault ] && /path/to/memento-vault/bin/memento-vault warmup >/dev/null 2>&1
```

You can also run it manually:

```bash
memento-vault warmup
```

## Obsidian (optional)

The installer copies Obsidian config and Base views into the vault. Open `~/memento` as a vault and you get:

- Graph view showing how notes connect
- Base views: by type, by project, recent, decisions, bugfixes
- Daily notes pointed at `fleeting/`
- Wikilink navigation between notes

## Uninstall

```bash
cd memento-vault
./uninstall.sh
```

Removes hooks, skills, and the agent from `~/.claude/`. Your vault and notes stay untouched.

## How it works

Full flow diagram in [docs/how-it-works.md](docs/how-it-works.md).

## License

MIT
