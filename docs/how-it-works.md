# How It Works

Memento Vault captures knowledge from Claude Code sessions, makes it searchable, and injects relevant notes back into active sessions.

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

## Tenet (retrieval) -- experimental

> Requires `./install.sh --experimental` and QMD.

Past knowledge flows back into active sessions via three hooks:

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

All three hooks are zero-cost when they have nothing relevant to say -- no output, no context overhead. When they do inject, overhead is ~139 input units per session on average. See [performance-analysis.md](performance-analysis.md) for benchmarks.

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

## Inception (background consolidation)

The triage agent captures one session at a time. It can't see that you've hit the same React testing footgun three times, or that every project's caching layer ends up needing the same TTL pattern. Inception fills this gap: it clusters notes by embedding similarity and synthesizes higher-order pattern notes that surface cross-session themes.

### Why this matters

Every knowledge system hits the same scaling problem: as notes accumulate, retrieval gets noisier and you lose the forest for the trees. The research converges on a solution -- periodic consolidation that promotes episodic memories into semantic abstractions:

- **Generative Agents** (Park et al., 2023): Removing reflections degraded agent believability by ~10%. Higher-level abstractions serve as efficient retrieval anchors for clusters of related memories.
- **Honcho** (Plastic Labs): Their "Dreamer" -- architecturally identical to Inception -- is the key ingredient in achieving 90.4% on the LongMem benchmark. Front-loading synthesis makes retrieval faster and cheaper over time.
- **A-MEM** (NeurIPS 2025): Zettelkasten-inspired self-organizing memory with synthesized evolution showed "superior improvement against existing SOTA baselines" across six foundation models.
- **CraniMem** (2026): Selective consolidation with importance weighting outperforms both unlimited retention and aggressive compression.

### How it works

```
Session ends
    |
    v
memento-triage.py (existing)
    |
    +---> maybe_trigger_inception()
          |
          +---> load inception-state.json
          +---> count notes newer than last run
          +---> >= 5 new notes? spawn detached inception process
                |
                v
          memento-inception.py (background, non-blocking)
                |
                +---> Phase 1: Local clustering (zero LLM cost)
                |     Read QMD embeddings from SQLite
                |     Mean-pool chunk vectors -> doc-level 768-dim vectors
                |     HDBSCAN clustering (leaf method, cosine metric)
                |     Score clusters: size + tag diversity + temporal spread
                |                     + project diversity + mean certainty
                |     Three-layer dedup:
                |       1. synthesized_from ledger (skip already-consolidated)
                |       2. Title token overlap (skip near-duplicates)
                |       3. LLM SKIP response (trivial connections rejected)
                |
                +---> Phase 2: LLM synthesis (codex or claude)
                |     For each cluster: build prompt with source notes
                |     LLM returns JSON {title, body, tags, certainty, related}
                |     or "SKIP" if connection is trivial
                |
                +---> Write pattern notes (atomic: tempfile + rename)
                +---> Backlink source notes (append to ## Related)
                +---> vault-commit.sh + QMD reindex
                +---> Save state to inception-state.json
```

### What it produces

Pattern notes in `notes/` with `source: inception`:

```yaml
---
title: Redis TTL misconfiguration is the recurring cache footgun
type: pattern
tags: [redis, caching, ttl, production]
source: inception
certainty: 3
synthesized_from:
  - redis-cache-requires-explicit-ttl
  - redis-session-store-eviction-policy
  - api-response-cache-invalidation
date: 2026-03-22T14:16
---

Three separate sessions dealt with Redis caching failures. The common
thread is missing or misconfigured TTL settings causing stale reads...
```

Pattern notes are first-class vault citizens -- they show up in search, briefings, and recall like any other note. They don't replace their sources (non-destructive). The `synthesized_from` field provides provenance for dedup on future runs.

### Performance

**Trigger overhead** (~10-20ms per SessionEnd): `maybe_trigger_inception()` loads a JSON file and globs the notes directory. Runs inside an already-async hook -- invisible to you.

**Full pipeline** (~2-6 minutes when triggered): Dominated by sequential LLM calls (~10-30s each). HDBSCAN clustering on 550 notes takes <1 second. Memory overhead is <50MB. Runs as a fully detached process -- can't affect the terminal.

| Phase | Time |
|-------|------|
| Trigger check | 10-20ms |
| Note collection + embedding load | 300-800ms |
| HDBSCAN clustering (550 notes) | 200-800ms |
| Scoring + dedup | 10-50ms |
| LLM synthesis (10 clusters) | 100-300s |
| File writes + backlinks | 50-200ms |

### Retrieval noise

Pattern notes compete with atomic notes for limited injection slots (`recall_max_notes: 3`). This is the right tradeoff:

- Pattern notes match broader queries because their embedding is a mean of the cluster. When a pattern note displaces an atomic note, BM25/vsearch scored it higher -- it's more relevant to the prompt.
- One injection gives you the synthesized insight *and* wikilinks to specifics.
- At ~200-300 chars per injection, pattern notes stay within the vault's injection budget (~555 chars/session average).
- Certainty 4-5 pattern notes are immune to temporal decay, so they persist as stable retrieval anchors -- the same behavior Park et al. validated as beneficial.

### Cost

Inception's cost depends on the LLM backend. The local phases (embedding extraction, HDBSCAN, scoring, dedup) are free.

**How often does it fire?** Trigger threshold is 5 new notes. Based on real vault data (560 notes over 14 active days, median 50 notes/day):

| Usage pattern | Notes/day | Runs/day | LLM calls/day | Runs/month |
|---------------|-----------|----------|----------------|------------|
| Light (2-3 sessions) | 5-10 | ~0.5 | ~5 | ~15 |
| Moderate (5-8 sessions) | 15-30 | ~1 | ~10 | ~30 |
| Heavy (10+ sessions) | 40-65 | ~2-3 | ~20-30 | ~60-90 |

**Cost per run** (10 clusters, ~5 notes/cluster, ~1900 input + 500 output per call):

| Backend | Per cluster | Per run (10) | Monthly (30 runs) | Monthly (90 runs) |
|---------|------------|-------------|-------------------|-------------------|
| **Codex (subscription)** | $0.00 | $0.00 | **$0.00** | **$0.00** |
| Haiku | $0.004 | $0.04 | $1.06 | $3.18 |
| Sonnet | $0.013 | $0.13 | $3.96 | $11.88 |

With the codex backend (subscription-covered), Inception has zero marginal cost. For API backends, Haiku is the practical choice at ~$1-3/month. Sonnet only makes sense if synthesis quality matters more than cost.

For context, a single concierge agent search costs ~$0.02 in API calls. Inception at 30 runs/month on Haiku ($1.06) replaces manual vault review that would cost far more in human attention.

### Staleness, dedup, and noise control

Pattern notes follow the same lifecycle as all vault notes:

- **Certainty capped at 3.** Inception notes start at certainty 3 ("confirmed by cross-referencing"), not the LLM's self-assessment. They're subject to temporal decay (90-day half-life) and defrag archival. If a pattern proves durable, bump it to 4-5 manually -- that's a human signal the system can trust.
- **Hybrid incremental clustering.** Each run clusters ALL notes (not just new ones) so cross-temporal patterns get detected. A new note that completes a cluster with two older notes produces a pattern. Only clusters containing at least 1 new note or flagged for refresh get synthesized -- the rest are skipped.
- **Pattern refresh.** When new notes join an existing pattern's cluster (superset of `synthesized_from`), the pattern is re-synthesized with the full evidence. Stale conclusions get updated as new evidence arrives.
- **Three-layer dedup.** Before writing: (1) check the `synthesized_from` ledger to skip already-covered clusters, (2) check title overlap against all existing notes, (3) the LLM itself responds SKIP for trivial connections. These layers prevent the vault from filling with redundant patterns.
- **Tenet filtering.** The retrieval hooks apply `min_score` thresholds, project filtering, temporal decay, and slot caps. Low-quality pattern notes get the same treatment as low-quality atomic notes -- they fade from injections over time and eventually get archived by defrag.

### Limitations

- **No invalidation of wrong patterns.** Inception can refresh a pattern when new evidence extends it, but it can't detect when a pattern's conclusion has been contradicted. If the source notes are archived or superseded by newer work, the pattern note persists until you delete it. Periodic `--full` runs re-cluster everything and may produce updated patterns, but there's no automated "this is now wrong" signal. This would require the LLM to evaluate its own past output against new evidence -- an open research problem.
- **No cross-system dedup.** The triage agent and Inception are independent pipelines. Both can write notes covering similar ground -- an atomic note "Redis TTL matters" and a pattern note "Cache TTL is the recurring footgun" may coexist. The Tenet hooks resolve this at query time (higher-scoring note wins the injection slot), but both consume index space.
- **Clustering depends on QMD embeddings.** Semantically similar notes using different vocabulary may not cluster together. The 768-dim model captures meaning reasonably well but isn't perfect.
- **HDBSCAN has tuning parameters.** `min_cluster_size=3` and `leaf` selection work well for ~550 notes but may need adjustment past 1000+.
- **Sequential LLM calls.** Clusters are synthesized one at a time. Parallelizing would cut runtime from minutes to under a minute but adds complexity.
- **First-run bias.** On a full backfill, the LLM sees all clusters at once and may over-synthesize. Incremental runs (5+ new notes) produce more focused patterns.
- **Cost scales with cluster count, not vault size.** A 5000-note vault with 10 clusters costs the same as a 500-note vault with 10 clusters. But more notes may produce more clusters, increasing cost per run.

### Configuration

```yaml
# --- Inception (background consolidation) ---
inception_enabled: false          # opt-in, zero code runs if false
inception_backend: codex          # "codex" (subscription) or "claude" (API)
inception_threshold: 5            # new notes before triggering
inception_min_cluster_size: 3     # HDBSCAN minimum cluster size
inception_max_clusters: 10        # max patterns per run
inception_cluster_threshold: 0.7  # HDBSCAN epsilon (higher = fewer clusters)
inception_exclude_tags: []        # tags to skip
inception_dry_run: false          # preview mode
```

Use `/inception` to run manually, or `/inception --dry-run` to preview clusters without writing.

## The skills

| Skill | What it does |
|-------|-------------|
| `/memento` | Capture insights from the current session |
| `/inception` | Find cross-session patterns and synthesize pattern notes |
| `/memento-defrag` | Archive stale notes (low certainty, old bugfixes, superseded) |
| `/start-fresh` | Capture session + save pending work + prompt to clear context |
| `/continue-work` | Recover context from git state, MEMORY.md, and optionally the vault |

## The concierge agent

A lightweight Haiku agent that searches the vault read-only. Spawned by `/continue-work` when local context is thin, or when you ask about past decisions.

Uses QMD for semantic search, falls back to grep if QMD is not installed.

## Defrag (knowledge decay)

Notes accumulate. `/memento-defrag` handles decay:

- Certainty 1-2 notes older than 60 days -> archive
- Bugfixes older than 90 days -> archive
- Superseded notes -> archive
- Hub notes (linked by 3+ others) -> never archive
- Certainty 4-5 -> never archive

Archived notes move to `archive/`, are removed from the QMD index, but remain in git history and are searchable via grep.
