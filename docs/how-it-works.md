# How It Works

Memento Vault captures knowledge from your Claude Code sessions automatically and makes it searchable.

## The flow

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
