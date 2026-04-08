#!/usr/bin/env bash
# Creates GitHub releases for v1.0.0–v1.3.0 and publishes the v2.0.0 draft.
# Requires: gh auth login (or GITHUB_TOKEN set)
set -euo pipefail

REPO="sandsower/memento-vault"

echo "==> Creating v1.0.0 release..."
gh release create v1.0.0 \
  --repo "$REPO" \
  --title "v1.0.0" \
  --notes "$(cat <<'EOF'
Persistent knowledge for Claude Code. SessionEnd hook triages transcripts into fleeting one-liners or atomic Zettelkasten notes with epistemic metadata. Everything lives in a git-backed vault you own.

## Capture
- `memento-triage.py` — SessionEnd hook parses Claude transcripts, gates on keyword density, writes fleeting or atomic notes with frontmatter (certainty, tags, source, epistemic status)
- `memento-sweeper.py` — prunes stale fleeting notes on schedule
- `vault-commit.sh` — auto-commits vault changes after triage

## Skills
- `/memento` — concierge search agent for the vault
- `/memento-defrag` — consolidate and deduplicate notes
- `/start-fresh` — blank-slate session with vault context
- `/continue-work` — resume from last session

## Infra
- `install.sh` — one-command setup, hooks + skills + vault scaffold
- Config-driven triage thresholds via `memento.yml`
- Optional QMD semantic search via `qmd-collection.yml`
- Optional Obsidian views (by-project, by-tag, by-source, recent, decisions, bugfixes)
- Extension points: `project_rules`, `extra_qmd_collections`, post-capture hook

## Stats
- 34 files, 2225 LOC
- 4 skills, 3 hooks, 1 agent
EOF
)"

echo "==> Creating v1.1.0 release..."
gh release create v1.1.0 \
  --repo "$REPO" \
  --title "v1.1.0" \
  --notes "$(cat <<'EOF'
Retrieval pipeline. Notes go in, now they come back out. Three new hooks inject vault context into every prompt — session briefing, mid-session recall, and file-aware tool context. All gated behind `--experimental`.

## Retrieval hooks
- `vault-briefing.py` — deferred session briefing with background vsearch, stale-file cleanup (>60s)
- `vault-recall.py` — per-prompt recall via BM25 + QMD, project-scoped filtering, skip patterns for non-user prompts
- `vault-tool-context.py` — file-read-aware context injection with cooldown gating

## Search enhancements
- Temporal decay: 90-day half-life, certainty 4–5 immune, certainty 3 half-rate
- Wikilink expansion: 1-hop follow on `[[wikilinks]]`, linked notes enter at 50% parent score
- Project-scoped filtering: notes tagged to other projects excluded, untagged notes pass through
- BM25 query augmentation with project slug for mid-specificity prompts

## Performance
- 5 skip patterns eliminate ~47% of wasted QMD calls (skill expansions, XML tags, long prompts, skill headers)
- Parallel search via `ThreadPoolExecutor` across primary + extra collections (~1200ms saved)
- `tool_context_cooldown` 3s → 1s, `tool_context_min_score` 0.75 → 0.65
- `wikilink_max_expanded` capped at 3 to prevent hub note flooding

## Infra
- `--experimental` flag: stable install gets capture only, experimental adds retrieval
- `memento_utils.py` — shared retrieval library (663 LOC)
- Debug-gated JSONL retrieval logging at `~/.config/memento-vault/retrieval.jsonl`
- Replay benchmark for real session performance analysis

## Stats
- Recall latency: 792ms mean (30 sessions, 381 prompts)
- 626x cost efficiency vs concierge search
- 13 files changed, +2849 LOC
EOF
)"

echo "==> Creating v1.2.0 release..."
gh release create v1.2.0 \
  --repo "$REPO" \
  --title "v1.2.0" \
  --notes "$(cat <<'EOF'
Nolan trilogy complete. Memento (capture) → Tenet (retrieval) → Inception (consolidation). Six zero-cost Tier 1 retrieval features close ~70% of the gap with Honcho's Dialectic prefetch. Inception clusters the vault and synthesizes pattern notes in the background. Both behind `--experimental`.

## Tenet — Tier 1 retrieval (~50ms added latency)
- PRF query expansion: two-pass BM25 with term extraction
- Wikilink graph + PageRank centrality boost (NetworkX, `/tmp` cached)
- Personalized PageRank expansion (replaces naive 1-hop wikilinks)
- RRF hybrid search: fuses BM25 + vsearch when warm
- Concept index from Inception: keyword → pattern note O(1) lookup
- Project retrieval maps from Inception: per-project note rankings

## Inception — background consolidation
- HDBSCAN clustering on QMD embeddings, synthesizes cross-session pattern notes
- Pipeline: collect → embed → cluster → score → dedup → synthesize → write
- Runs detached after session triage, fully opt-in via `inception_enabled`
- Certainty capped at 3 (subject to decay, user upgrades manually)
- Three-layer dedup: ledger + title overlap + LLM SKIP
- Hybrid incremental: clusters ALL notes, synthesizes only new/refresh
- Zero marginal cost with codex backend
- `/inception` skill for manual trigger

## Docs
- Industry comparison rewrite: honest positioning vs Honcho, CraniMem, context rot literature
- Updated architecture docs with Tenet pipeline flow and Inception lifecycle

## Stats
- 180 tests (86 new)
- 36 files changed, +5901 LOC
EOF
)"

echo "==> Creating v1.3.0 release..."
gh release create v1.3.0 \
  --repo "$REPO" \
  --title "v1.3.0" \
  --notes "$(cat <<'EOF'
Adaptive pipeline and real benchmarks. Recall latency drops from 3262ms to 443ms. LongMemEval baseline lands at NDCG@10 = 0.892 against the standard 500-question memory benchmark.

## Tier 2 — cross-encoder reranking
- MiniLM-L-6-v2 via ONNX Runtime re-scores (query, candidate) pairs after Tier 1
- Lazy model download from HuggingFace Hub, cached in \`~/.cache/memento-vault/models/\`
- All deps optional (\`onnxruntime\`, \`tokenizers\`, \`huggingface_hub\`), falls through silently when missing

## Adaptive pipeline
- BM25 runs first. Top score ≥ 0.55 → skip PRF, RRF, reranker, go straight to enhance
- Deep path only fires for low-confidence queries where BM25 genuinely needs help
- PRF accepts pre-fetched results (eliminates redundant QMD subprocess call)
- Graph pre-built at session start

## LongMemEval benchmark
- \`longmemeval_adapter.py\` — runs Tenet retrieval against 500-question standard benchmark using \`rank_bm25\` (no QMD subprocess overhead)
- \`optuna_sweep.py\` — 11-dim TPE search with MedianPruner and SQLite checkpoint/resume (~960 trials in 8h)
- \`apply_sweep_results.py\` — patches config defaults from Optuna sweep_results.json
- \`--mode full\` — end-to-end eval: retrieval → codex generation → codex judging, zero API cost
- Baseline: NDCG@10 = 0.892, MRR = 0.907, recall@5 = 0.909

## Performance
- Recall: 472ms mean (was 792ms v1.1.0, 3262ms pre-adaptive)
- Wall-clock overhead down 25% from v1.1.0 despite Tier 1+2 features
- Updated industry comparison with Honcho 3 (92.6% LongMemEval), Hindsight, Letta V1, Google AOMA, MemOS 2.0

## Stats
- 250 tests (47 new)
- 20 files changed, +2739 LOC

## Upgrade from v1.2

\`\`\`
cd memento-vault && git pull && ./install.sh --experimental
\`\`\`
EOF
)"

echo "==> Publishing v2.0.0 draft..."
gh release edit v2.0.0 \
  --repo "$REPO" \
  --draft=false

echo "Done. All releases published."
