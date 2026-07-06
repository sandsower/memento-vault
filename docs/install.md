# Install

## Homebrew tap

```bash
brew tap sandsower/tap
brew install memento-vault
memento-vault install
```

## Git/manual install

```bash
git clone https://github.com/sandsower/memento-vault.git
cd memento-vault
./install.sh
```

Creates the vault at `~/memento`, copies hooks and the `memento/` package into `~/.claude/`, optionally sets up Obsidian views and QMD search.
Works on Linux and macOS.

Custom vault path:

```bash
MEMENTO_VAULT_PATH=~/my-vault ./install.sh
```

The installer also links the `memento-vault` CLI into `~/.local/bin` when possible, so future updates can use `memento-vault update`.
If `~/.local/bin` is not on your `PATH`, either add it or run the repository-local `./bin/memento-vault` directly.

## Reinstalling and forcing

To safely rerun setup for the same version without discarding local edits:

```bash
memento-vault install --reinstall
```

`--force` is reserved for recovery from broken installed files.
It overwrites memento-managed files and requires confirmation, or `MEMENTO_FORCE=1` in non-interactive environments.

## Health and doctor

Check local vault/install health at any time:

```bash
memento-vault health               # concise read-only diagnostics
memento-vault doctor               # alias for health
memento-vault retrieval-report      # local retrieval debug dashboard/report with recommendations
memento-vault health --json        # structured output for automation
memento-vault health --deep        # opt-in live integration probes
memento-vault retrieval-report --html --output /tmp/retrieval.html
```

Warnings exit 0 by default; failures exit 1.
Use `--strict` if automation should fail on warnings too.
The command is read-only: it reports suggested repairs such as `./install.sh --reinstall`, but does not modify vault/config files.
JSON output includes `automation_memory` readiness metadata for runner probes without contacting remote services by default.
Add `--deep` to run bounded live probes against configured integrations.

The legacy `tools/vault-health-check.sh` script remains available for direct callers that need low-level structural content checks (required vault directories, note frontmatter, wikilinks, filename conventions, and git presence).
Prefer `memento-vault health`/`doctor` for operational install/runtime diagnostics; use the legacy script only when you specifically want those structural vault-content checks.

## Full install (hooks + retrieval + consolidation)

The base install captures knowledge and injects it back into active sessions.
To also add background consolidation and the orra-init skill:

```bash
./install.sh --experimental
```

This adds extra modules:

- **Inception** -- background consolidation that clusters notes and synthesizes cross-session patterns
- **orra-init** -- experimental helper skill for starting new agents/workflows

Both require QMD.
Inception also needs `pip install numpy hdbscan scikit-learn`.
See [docs/how-it-works.md](how-it-works.md) for details.

## MCP install (hookless agents)

For agents that support MCP but not native hooks (Cursor, Windsurf, etc.):

```bash
./install.sh --mcp
```

This installs the `memento/` package, writes generic MCP server config, and registers the server with Claude Code and Codex when those CLIs are installed.
The server runs over stdio via `python -m memento`.
The installer verifies the `mcp` Python package is available and installs it if needed.
Claude Code gets Claude-specific skills and the concierge agent under `~/.claude`; Codex gets agent-agnostic skills under `~/.codex/skills`.

You can combine flags: `./install.sh --experimental --mcp` gives you the base hooks/context + MCP, plus Inception/orra-init extras.

Remote flags (`--remote <url>`) are covered in [docs/remote-deployment.md](remote-deployment.md).

## Upgrading from v1.x

The installer is version-aware.
Modified hooks are preserved with `.new` copies for manual diffing.
On subsequent upgrades, modified files are auto-merged via three-way merge (`git merge-file`).
Existing opt-outs in your Claude/Pi config continue to win; rerun `./install.sh` to pick up the default hook set after upgrading.

```bash
cd memento-vault && git pull && ./install.sh
```

## Requirements

- Python 3.10+
- Git
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (for hook-based setup)
- [QMD](https://github.com/tobi/qmd) (optional, semantic search)
- [Obsidian](https://obsidian.md) (optional, browsing)
- `mcp` Python package (for MCP setup, installed automatically by `--mcp`)

## Portable vault archives

Use portable archives when moving a vault between machines or safely merging vault state without relying on a raw filesystem copy.
Archives keep Markdown as the canonical source of truth and include vault identity, `notes/`, `fleeting/`, `projects/`, `archive/`, sync ledger state, and tombstones; derived search indexes and embeddings are excluded.

```bash
memento-vault archive export --vault ~/memento ./memento-portable.zip
memento-vault archive import --vault ~/memento-restored ./memento-portable.zip
```

Imports default to safe conflict errors on existing divergent files.
Use `--conflict skip` to keep local files or `--conflict overwrite` when intentionally replacing them.
Tombstones are merged during import so deleted notes do not reappear during later archive imports or remote sync catch-up.

## Model warmup

Tenet's deferred briefing search uses vector search, which requires loading an embedding model.
First call after a reboot takes 6-8s; subsequent calls are ~1.5s (model stays in OS page cache).
The installer can add a background warmup to your shell rc file so the model is always cached:

```bash
# Added to .zshrc/.bashrc by the installer (optional)
[ -x /path/to/memento-vault/bin/memento-vault ] && /path/to/memento-vault/bin/memento-vault warmup >/dev/null 2>&1
```

You can also run it manually:

```bash
memento-vault warmup
```

## Uninstall

```bash
cd memento-vault
./uninstall.sh
```

Removes hooks, skills, and the agent from `~/.claude/`.
Your vault and notes stay untouched.
