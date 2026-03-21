# Configuration

Memento Vault is configured via a YAML file. The installer creates it at `~/.config/memento-vault/memento.yml`.

## Config file locations (checked in order)

1. `~/memento/memento.yml` (vault root)
2. `~/.config/memento-vault/memento.yml`
3. `~/.memento-vault.yml` (home directory)

The first file found wins. If none exist, defaults are used.

## Options

```yaml
# Where your vault lives
vault_path: ~/memento

# Sessions with more exchanges than this are "substantial"
# Substantial sessions spawn a background agent for atomic notes
exchange_threshold: 15

# Sessions editing more files than this are "substantial"
file_count_threshold: 3

# File patterns that force a session to be substantial
# (matched anywhere in the edited file path)
notable_patterns: [plan, design, MEMORY.md, CLAUDE.md, SKILL.md]

# QMD collection name (empty string disables QMD integration)
qmd_collection: memento

# Additional QMD collections to search
extra_qmd_collections: []

# Project rules (map directories to slugs and ticket patterns)
project_rules: []

# Auto-commit vault changes after triage
auto_commit: true

# Claude model for generating atomic notes
agent_model: sonnet

# Seconds to wait before committing agent-written notes
agent_delay_seconds: 90
```

## Project rules

Map working directories to project slugs and ticket patterns. Without rules, the slug is the directory name and tickets are extracted by a generic `[a-z]+-\d+` regex.

```yaml
project_rules:
  - path_contains: "my-company.git"
    slug: "my-company"
    ticket_pattern: "(PROJ-\\d+)"
  - path_contains: "side-project"
    slug: "side-project"
```

Rules are checked in order. First match wins. `ticket_pattern` is optional.

## Extra QMD collections

Search additional QMD collections alongside the main memento vault. The concierge agent and the delta-check gate both use these.

```yaml
extra_qmd_collections: [team-knowledge, shared-docs]
```

Each collection must be configured in your `~/.config/qmd/index.yml`.

## Post-capture extensions

The `/memento` skill checks for `~/.claude/skills/memento-post/SKILL.md` after creating notes. If the file exists, its instructions run as an extra step. Use this for project-specific workflows like promoting notes to a team vault or tagging with domain-specific labels.

## Tuning the triage

The triage decides which sessions are worth capturing as atomic notes vs just fleeting one-liners.

**More aggressive capture** (capture more sessions):
```yaml
exchange_threshold: 8
file_count_threshold: 2
notable_patterns: [plan, design, MEMORY.md, CLAUDE.md, SKILL.md, test, spec, config]
```

**Less aggressive capture** (fewer notes, less noise):
```yaml
exchange_threshold: 25
file_count_threshold: 5
notable_patterns: [plan, design]
```

The delta-check gate (QMD-powered) already prevents duplicate captures regardless of these thresholds. If QMD says the vault already covers a topic and no new files were edited, the agent is not spawned.

## Disabling features

**No auto-commit** (commit manually):
```yaml
auto_commit: false
```

**No QMD** (grep-only search):
```yaml
qmd_collection: ""
```

**No background agent** (fleeting notes only):
Set `exchange_threshold` and `file_count_threshold` to very high numbers:
```yaml
exchange_threshold: 9999
file_count_threshold: 9999
```
