# How It Works

Memento Vault captures knowledge from your Claude Code sessions automatically, makes it searchable, and injects relevant knowledge back into active sessions.

## Capture flow (write path)

```
Session ends
    |
    v
SessionEnd hook fires
    |
    v
memento-triage.py reads the transcript
    |
    +---> write_fleeting()
    |     One-liner in fleeting/YYYY-MM-DD.md
    |     (always runs, zero LLM cost)
    |
    +---> append_session_to_project()
    |     Session line in projects/{project-slug}.md
    |
    +---> is_substantial()?
    |     Exchange count > 15, or files edited > 3, or notable file patterns
    |
    +---> has_new_insight()?
    |     QMD delta-check: does the vault already cover this topic?
    |     (falls back to "yes" if QMD not installed)
    |
    +---> YES to both: spawn background Claude agent
    |     Reads transcript, creates atomic notes in notes/
    |     Each note: YAML frontmatter + 2-5 sentence body + wikilinks
    |
    +---> vault-commit.sh auto-commits
    |
    +---> QMD reindex (if installed)
```

## Retrieval flow (read path) — experimental

> Requires `./install.sh --experimental` and QMD.

Knowledge flows back into active sessions via three hooks:

```
Session starts
    |
    v
vault-briefing.py (SessionStart hook)
    |
    +---> detect project from cwd + git branch
    +---> SYNC: read projects/{slug}.md for recent sessions (<50ms)
    +---> print [vault] project + sessions to stdout --> Claude sees it
    +---> ASYNC: spawn background subprocess for QMD vsearch
    |     writes results to /tmp/memento-deferred-briefing.json
    |     picked up by vault-recall.py on the first prompt
    |
    v
User types a prompt
    |
    v
vault-recall.py (UserPromptSubmit hook)
    |
    +---> consume deferred briefing results (if ready)
    +---> relevance gate: skip short prompts, confirmations, skill invocations,
    |     command messages, prompts >500 chars
    +---> BM25 search against prompt + project slug
    +---> enhance_results():
    |       temporal decay (90-day half-life, certainty 4-5 immune)
    |       project filter (exclude notes from other projects)
    |       wikilink expansion (1-hop, 50% parent score, cap 3)
    +---> dedup: skip if same top result as last injection (within 3 prompts)
    +---> print [vault] related memories to stdout --> Claude sees them
    |
    v
Claude reads a file
    |
    v
vault-tool-context.py (PreToolUse hook, Read matcher)
    |
    +---> skip check: system paths, vendor dirs, config files, assets
    +---> session injection cap (max 5)
    +---> directory cache check (hit = use cached, miss = continue)
    +---> cooldown check (1s between QMD calls)
    +---> extract keywords from file path (split camelCase, filter stop segments)
    +---> BM25 search against keywords
    +---> enhance_results() pipeline
    +---> dedup against recall + prior tool-context injections
    +---> return JSON with additionalContext --> Claude sees it before the file
```

All three hooks are zero-cost when they have nothing relevant to say — they produce no output and Claude's context is unchanged. When they do inject, overhead is ~139 input units per session on average. See [performance-analysis.md](performance-analysis.md) for detailed benchmarks.

## What gets captured

**Fleeting notes** (every session with 2+ exchanges):
- Timestamp, session ID, working directory, branch
- Exchange count, files edited count
- First 100 chars of the first user prompt

**Atomic notes** (substantial sessions with new insights):
- One idea per note, slugified filename
- YAML frontmatter with epistemic metadata (certainty, validity-context)
- Cross-linked via `[[wikilinks]]`
- Types: decision, discovery, pattern, bugfix, tool

**Project indexes** (one per project):
- Links to all notes about this project
- Session history with timestamps

## The skills

| Skill | What it does |
|-------|-------------|
| `/memento` | Manually capture insights from the current session |
| `/memento-defrag` | Archive stale notes (low certainty, old bugfixes, superseded) |
| `/start-fresh` | Capture session + save pending work + prompt to clear context |
| `/continue-work` | Recover context from git state, MEMORY.md, and optionally the vault |

## The concierge agent

A lightweight Haiku agent that searches the vault read-only. Spawned by `/continue-work` when local context is thin, or when you ask about past decisions.

Uses QMD for semantic search, falls back to grep if QMD is not installed.

## Defrag (knowledge decay)

Notes accumulate. The `/memento-defrag` skill handles decay:

- Certainty 1-2 notes older than 60 days -> archive
- Bugfixes older than 90 days -> archive
- Superseded notes -> archive
- Hub notes (linked by 3+ others) -> never archive
- Certainty 4-5 -> never archive

Archived notes move to `archive/`, are removed from the QMD index, but remain in git history and are searchable via grep.
