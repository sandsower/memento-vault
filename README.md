# Memento Vault

Persistent knowledge capture for coding agents. Sessions get triaged, scored, and filed as searchable Zettelkasten notes. No cloud services, no databases. Markdown and git.

Works with Claude Code (native hooks), and any MCP-compatible agent (Cursor, Windsurf, Codex, etc.) via the built-in MCP server.

## What it does

When a coding session ends, the triage pipeline reads the transcript and decides what to keep. Short sessions get a one-liner in a daily log. Substantial ones spawn a background agent that writes atomic notes with YAML frontmatter, wikilinks, and epistemic metadata (how confident is this note? what would make it wrong?). Everything lives in a local git repo you can browse with Obsidian or search with QMD.

For agents without native hook support, the MCP server exposes the same operations as tools: search the vault, store notes, capture sessions, read specific notes.

## Install

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

### Full install (hooks + retrieval + consolidation)

The base install captures knowledge. To also inject knowledge back into active sessions and enable background consolidation:

```bash
./install.sh --experimental
```

This adds two modules:

- **Tenet** -- three retrieval hooks that inject vault notes into active sessions (briefing, recall, tool context)
- **Inception** -- background consolidation that clusters notes and synthesizes cross-session patterns

Both require QMD. Inception also needs `pip install numpy hdbscan scikit-learn`. See [Tenet](#tenet) and [Inception](#inception) for details.

### MCP install (hookless agents)

For agents that support MCP but not native hooks (Cursor, Windsurf, etc.):

```bash
./install.sh --mcp
```

This installs the `memento/` package and writes MCP server config. The server runs over stdio via `python -m memento`. The installer verifies the `mcp` Python package is available and installs it if needed.

You can combine flags: `./install.sh --experimental --mcp` gives you hooks + retrieval + MCP.

### Upgrading from v1.x

The installer is version-aware. Modified hooks are preserved with `.new` copies for manual diffing. On subsequent upgrades, modified files are auto-merged via three-way merge (`git merge-file`).

```bash
cd memento-vault && git pull && ./install.sh --experimental
```

### Requirements

- Python 3.9+
- Git
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (for hook-based setup)
- [QMD](https://github.com/tobi/qmd) (optional, semantic search)
- [Obsidian](https://obsidian.md) (optional, browsing)
- `mcp` Python package (for MCP setup, installed automatically by `--mcp`)

## MCP server

The MCP server exposes 5 tools over stdio. Any MCP-compatible agent can use them.

| Tool | What it does |
|------|-------------|
| `memento_search` | Search vault notes (BM25, semantic, RRF fusion, temporal decay, PageRank boost) |
| `memento_store` | Write a single knowledge note with frontmatter and project indexing |
| `memento_capture` | End-of-session triage: parse transcript or accept a summary, write fleeting + atomic note |
| `memento_get` | Read a specific note by name or path |
| `memento_status` | Vault health: note count, project count, config summary |

Run manually:

```bash
python -m memento
```

Or configure your agent's MCP settings to spawn it as a subprocess:

```json
{
  "memento-vault": {
    "command": "python3",
    "args": ["-m", "memento"],
    "env": {"PYTHONPATH": "~/.claude/hooks"}
  }
}
```

`memento_capture` is the MCP equivalent of the SessionEnd hook. Agents without hook support can call it at the end of a session with either a transcript path or a structured summary.

## Tenet

> Requires QMD.

Past knowledge flows back into active sessions via three hooks:

- **Session briefing** (SessionStart): injects your project's recent sessions and relevant vault notes when a session opens. Fast sync output (<50ms), QMD search deferred to background.
- **Prompt recall** (UserPromptSubmit): searches each prompt against the vault and surfaces matching notes before Claude processes it. Adaptive pipeline: fast BM25 path for confident matches, deep path (PRF, RRF, multi-hop wikilink-following, cross-encoder reranking) for low-confidence queries.
- **Tool context** (PreToolUse): injects vault notes when Claude reads files in known code areas. Directory-level BM25 with caching and rate limiting.

All three hooks stay silent when they have nothing relevant. Zero tokens injected on trivial prompts, config files, and vendor directories.

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

## Architecture

The `memento/` package is agent-agnostic. Seven modules handle config, search, graph algorithms, vault I/O, LLM abstraction, and type definitions. Hooks and MCP tools are thin wrappers around this package.

```
memento/
  config.py      Configuration loading, project detection
  search.py      QMD search, PRF, RRF, temporal decay, PageRank
  graph.py       Wikilink graph, PageRank, PPR expansion
  store.py       Vault I/O, write locking, dedup, note writing
  llm.py         5 backends: claude, codex, gemini, anthropic-api, openai-compat
  utils.py       Secret sanitization, tag normalization
  types.py       TypedDict definitions (SearchResult, NoteMetadata, SessionMeta)
  adapters/      Transcript parsing (Claude adapter, pluggable for others)
  mcp_server.py  MCP server (5 tools over stdio)
```

LLM backend is configurable:

```yaml
llm_backend: claude        # claude, codex, gemini, anthropic-api, openai-compat
llm_model: sonnet          # model name for the chosen backend
```

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
tool_context: true

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

## QMD (optional)

QMD adds semantic search over your vault. Without it the concierge agent falls back to grep, which handles keyword searches but misses conceptual matches. QMD is required for Tenet and Inception.

```bash
qmd search "caching strategy" -c memento
```

The concierge agent uses QMD automatically when you ask about past decisions.

### Model warmup

Tenet's deferred briefing search uses vector search, which requires loading an embedding model. First call after a reboot takes 6-8s; subsequent calls are ~1.5s (model stays in OS page cache). The installer can add a background warmup to your shell rc file so the model is always cached:

```bash
# Added to .zshrc/.bashrc by the installer (optional)
command -v qmd &>/dev/null && qmd vsearch "warmup" -c memento -n 1 &>/dev/null &
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
