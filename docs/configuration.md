# Configuration

Config file: `~/.config/memento-vault/memento.yml` (created by the installer).

## Config file locations (checked in order)

1. `~/memento/memento.yml` (vault root)
2. `~/.config/memento-vault/memento.yml`
3. `~/.memento-vault.yml` (home directory)

First file found wins. If none exist, defaults apply.

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

# --- Retrieval hooks ---
# Inject vault notes at session start
session_briefing: true
briefing_max_notes: 5
briefing_min_score: 0.3

# Inject vault notes before each prompt
prompt_recall: true
recall_min_score: 0.4
recall_max_notes: 3
recall_skip_patterns: ["^(yes|no|ok|sure|thanks)$", "^git\\s", "^run\\s"]
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

Checked in order. First match wins. `ticket_pattern` is optional.

## Extra QMD collections

Search additional QMD collections alongside the main vault. The concierge agent and the delta-check gate both use these.

```yaml
extra_qmd_collections: [team-knowledge, shared-docs]
```

Each collection must be configured in your `~/.config/qmd/index.yml`.

## Post-capture extensions

The `/memento` skill checks for `~/.claude/skills/memento-post/SKILL.md` after creating notes. If the file exists, its instructions run as an extra step. Use this for things like promoting notes to a team vault or applying domain-specific tags.

## Tuning the triage

The triage decides which sessions get atomic notes vs fleeting one-liners.

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

The delta-check gate (QMD-powered) prevents duplicate captures regardless of these thresholds. If QMD says the vault already covers a topic and no new files were edited, the agent is not spawned.

## Tenet — retrieval hooks

### Session briefing

At session start, `vault-briefing` injects a compact summary of your project's vault state into Claude's context. Includes recent sessions and the most relevant notes.

```yaml
# Disable the session briefing
session_briefing: false

# Show more/fewer notes
briefing_max_notes: 8

# Lower the threshold to surface more notes (default 0.3)
briefing_min_score: 0.2
```

Requires QMD. Falls back to project index notes if QMD is unavailable.

### Prompt recall

On every prompt, `vault-recall` runs a semantic search and injects matching vault notes. This is Tenet's just-in-time retrieval mechanism.

```yaml
# Disable prompt recall
prompt_recall: false

# Tighter relevance threshold (fewer, more relevant results)
recall_min_score: 0.6

# Show more results per prompt
recall_max_notes: 5

# Custom skip patterns (prompts matching these are never searched)
recall_skip_patterns: ["^(yes|no|ok)$", "^git\\s", "^npm\\s"]
```

Deduplication is automatic -- if the top result matches the last injection, it skips until 3 prompts have passed. Requires QMD.

## Disabling features

**No auto-commit** (commit manually):

```yaml
auto_commit: false
```

**No QMD** (grep-only search, no Tenet):

```yaml
qmd_collection: ""
```

**No Tenet** (capture only, no retrieval):

```yaml
session_briefing: false
prompt_recall: false
```

**No background agent** (fleeting notes only):

```yaml
exchange_threshold: 9999
file_count_threshold: 9999
```
