# Memento Vault

Persistent knowledge capture for Claude Code. Sessions get triaged, scored, and filed as searchable Zettelkasten notes. No cloud services, no databases. Markdown and git.

## What it does

A hook fires when a Claude Code session ends. It reads the transcript and decides what to keep. Short sessions get a one-liner in a daily log. Substantial ones spawn a background agent that writes atomic notes with YAML frontmatter, wikilinks, and epistemic metadata (how confident is this note? what would make it wrong?). Everything lives in a local git repo you can browse with Obsidian or search with QMD.

## Install

```bash
git clone https://github.com/sandsower/memento-vault.git
cd memento-vault
./install.sh
```

Creates the vault at `~/memento`, copies hooks and skills into `~/.claude/`, optionally sets up Obsidian views and QMD search. Works on Linux and macOS.

Custom vault path:

```bash
MEMENTO_VAULT_PATH=~/my-vault ./install.sh
```

### Experimental modules

The stable install captures knowledge. To also **inject knowledge back** into active sessions and enable background consolidation, install with the experimental flag:

```bash
./install.sh --experimental
```

This adds two modules:

- **Tenet** — three retrieval hooks that inject vault notes into active sessions (briefing, recall, tool context)
- **Inception** — background consolidation that clusters notes and synthesizes cross-session patterns

Both require QMD. Inception also needs `pip install numpy hdbscan scikit-learn`. See [Tenet](#tenet-experimental) and [Inception](#inception-experimental) for details.

### Requirements

- Python 3
- Git
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- [QMD](https://github.com/tobi/qmd) (optional, semantic search)
- [Obsidian](https://obsidian.md) (optional, browsing)

## Tenet (experimental)

> Requires `./install.sh --experimental` and QMD.

Past knowledge flows back into active sessions via three hooks:

- **Session briefing** (SessionStart): injects your project's recent sessions and relevant vault notes when a session opens. Fast sync output (<50ms), QMD search deferred to background.
- **Prompt recall** (UserPromptSubmit): BM25 keyword search on each prompt, surfaces matching notes before Claude processes it. Project-scoped, with temporal decay and wikilink expansion.
- **Tool context** (PreToolUse): injects vault notes when Claude reads files in known code areas. Directory-level BM25 with caching and rate limiting.

All three hooks stay silent when they have nothing relevant. Zero tokens injected on trivial prompts, config files, and vendor directories.

### Performance

Benchmarked against 30 real sessions (341 prompts, 362 file reads, 16 projects):

| Metric | Value |
|---|---|
| Avg injected per session | ~555 chars (~139 input units) |
| Effective hit rate | 100% (when hooks search, they find relevant notes) |
| Avg recall latency | 792ms per prompt |
| Avg tool-context latency | 141ms per file read |
| Session briefing | <83ms (deferred QMD search is non-blocking) |

Full analysis with methodology, industry comparison, and optimization details in [docs/performance-analysis.md](docs/performance-analysis.md).

## Inception (experimental)

> Requires `./install.sh --experimental`, QMD, and `pip install numpy hdbscan scikit-learn`.

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
Session ends -> triage hook fires
  -> always: write fleeting one-liner (no LLM cost)
  -> if substantial: spawn background agent for atomic notes
  -> delta-check: skip if vault already covers the topic
  -> auto-commit + QMD reindex
```

"Substantial" means 15+ exchanges, 3+ files edited, or touching notable files (plans, designs, CLAUDE.md). All thresholds are configurable.

### Note format

Each note has YAML frontmatter: certainty score (1-5), optional validity context, type (decision/discovery/pattern/bugfix/tool), wikilinks to related notes. Full schema in [docs/frontmatter-schema.md](docs/frontmatter-schema.md).

## Configuration

Config lives at `~/.config/memento-vault/memento.yml`. All options in [docs/configuration.md](docs/configuration.md).

```yaml
vault_path: ~/memento
exchange_threshold: 15
file_count_threshold: 3
qmd_collection: memento
auto_commit: true

# Tenet retrieval hooks (experimental)
session_briefing: true      # inject vault notes at session start
briefing_max_notes: 5
prompt_recall: true          # inject vault notes per prompt
recall_max_notes: 3
tool_context: true           # inject vault notes on file reads
tool_context_min_score: 0.65
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
