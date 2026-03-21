# Memento Vault

Persistent knowledge capture for Claude Code. Every session you close gets triaged, scored, and filed as searchable Zettelkasten notes. No cloud services, no databases. Just markdown and git.

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

### Requirements

- Python 3
- Git
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- [QMD](https://github.com/tobi/qmd) (optional, semantic search)
- [Obsidian](https://obsidian.md) (optional, browsing)

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
notable_patterns: [plan, design, MEMORY.md, CLAUDE.md, SKILL.md]
qmd_collection: memento
auto_commit: true
```

### Extension points

Three ways to layer project-specific behavior on top without forking:

- `project_rules` in config: map directories to project slugs and ticket patterns
- `extra_qmd_collections` in config: search additional QMD collections alongside the vault
- `~/.claude/skills/memento-post/SKILL.md`: post-capture hook that runs after `/memento` creates notes (e.g., promote to a team vault, apply domain tags)

## QMD (optional)

QMD adds semantic search over your vault. Without it the concierge agent falls back to grep, which works for keyword searches but misses conceptual matches.

```bash
qmd search "caching strategy" -c memento
```

The concierge agent uses QMD automatically when you ask about past decisions.

## Obsidian (optional)

The installer copies Obsidian config and Base views into the vault if you want them. Open `~/memento` as a vault and you get:

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
