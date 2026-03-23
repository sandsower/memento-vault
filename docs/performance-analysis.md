# Performance analysis

## TLDR

Recall latency: 472ms avg (adaptive pipeline skips expensive stages when BM25 is confident). Total session overhead: ~600 chars / ~150 input units. Effective hit rate: 100% (when hooks search, they find relevant notes). LongMemEval retrieval NDCG@10: 0.892. Optuna sweep confirmed current defaults are near-optimal. Zero LLM cost at query time.

## Current performance (v1.3.0+)

### Per-hook latency and injection

```
                        Latency         Injections      Chars/session
                        avg    p95      rate   eff.     avg
  ---------------------------------------------------------------
  Briefing (sync)       282ms  673ms    73%    73%      172
  Recall (adaptive)     472ms  1201ms   9%     100%     340
  Tool context (BM25)   230ms  645ms    5%     100%     86
```

**Effective hit rate** = injections / (calls - intentional skips). When the hooks actually search, they always find relevant notes. The low raw rates are by design: most calls are correctly skipped (short prompts, confirmations, config files).

### Skip breakdown

```
Recall (450 calls):
    Skipped:    408  (91%)   <- short prompts, confirmations, skill invocations
    Searched:    42  (9%)
    Injected:    42  (100% of searched)

Tool context (437 calls):
    Skipped:    416  (95%)   <- config files, node_modules, assets, vendor
    Searched:    21  (5%)
    Injected:    21  (100% of searched)
```

### Wall-clock overhead

| Stat | Value | % of 20min session |
|---|---|---|
| Mean | 9.8s | 0.8% |
| Median | 3.7s | 0.3% |
| P95 | 28.6s | 2.4% |
| Max | 52.3s | 4.4% |

**Blocking vs non-blocking:**

| Hook | Type | Impact |
|---|---|---|
| Briefing | Non-blocking (deferred to background) | Low, 282ms, user doesn't wait |
| Recall | Blocks prompt processing | Moderate, 472ms avg per prompt searched |
| Tool context | Blocks file read | Low, 230ms during Claude's autonomous tool use |

### Cost per session

```
Briefing:       172 chars   (29% of total)
Recall:         340 chars   (57% of total)
Tool context:    86 chars   (14% of total)
------------------------------------------------
Total:          597 chars   (~149 input units)
```

### Adaptive pipeline

The recall pipeline has two paths:

- **Fast path** (BM25 score >= 0.55): BM25 only, ~200ms. Handles ~34% of searched prompts.
- **Deep path** (BM25 score < 0.55): PRF expansion, RRF hybrid search, multi-hop wikilink-following, cross-encoder reranking. Handles ~66% of searched prompts.

This keeps average recall at 472ms despite running 6+ enhancement stages on the deep path.

### Multi-hop retrieval

Multi-hop follows `[[wikilinks]]` from top results to pull in connected notes. Fires on the deep path only.

- 98% of vault notes have wikilinks
- 80% of recalls have followable links in their result set
- Retrospective on 1000 real recalls: would add ~2 notes per enriched recall

### LongMemEval retrieval baseline

500 questions, BM25 + adaptive PRF:

| Metric | Score |
|---|---|
| NDCG@10 | **0.892** |
| NDCG@5 | 0.878 |
| MRR | 0.907 |
| Recall@1 | 0.550 |
| Recall@5 | 0.909 |
| Recall@10 | 0.942 |

An Optuna sweep (400 trials across 11 parameters) confirmed the current defaults are near-optimal. The sweep's best config scored 0.880, below production's 0.892. The gap comes from graph features (PPR, PageRank, concept index) that the synthetic benchmark can't exercise. See `benchmark/sweep-findings-2026-03-23.md` for full results.

## Inception pipeline

Inception is a background consolidation agent that clusters vault notes by embedding similarity and produces pattern notes. Runs at `SessionEnd` (fully detached), never blocks the user.

### Timing (619 notes)

| Phase | Time |
|---|---|
| Note collection + frontmatter parsing | 200-500ms |
| QMD embedding extraction (SQLite) | 100-300ms |
| HDBSCAN clustering (619 x 768-dim) | 200-800ms |
| Scoring + dedup | 10-50ms |
| LLM synthesis (10 clusters, 4 parallel workers) | 30-90s **(dominates)** |
| File writes + backlinks | 50-200ms |
| **Total** | **~30-90s, fully detached** |

### Resource usage

| Metric | Value |
|---|---|
| Memory overhead | <50MB (embedding matrix + HDBSCAN) |
| Pattern notes per run | Up to `inception_max_clusters` (default 10) |
| LLM cost | Zero with Codex subscription; ~$1-3/month with Haiku |
| Disk writes | 1 pattern note per cluster (~500 bytes each) + backlink appends |

## Comparison: hooks vs concierge

| Dimension | Hooks (automatic) | Concierge (on-demand) |
|---|---|---|
| Input units per use | ~149/session | ~72,500/call |
| Latency | 472ms/prompt (blocking) | 60-90s (one-time) |
| Trigger | Every prompt + file read | Manual, when someone asks |
| Context depth | One-liner breadcrumbs | Full narrative synthesis |
| Coverage | Every session, always-on | Only when invoked |

One concierge call costs the same as **486 hooked sessions**. They're complementary: hooks prevent concierge calls by jogging memory with breadcrumbs.

## Comparison: industry systems

| System | Architecture | Retrieval | Consolidation | LLM cost | Storage |
|---|---|---|---|---|---|
| **Memento-vault** | BM25/vector hooks + CE reranker | 472ms adaptive, PRF + RRF + PPR + PageRank + MiniLM-L6 | Inception (batch HDBSCAN, parallel) | Zero at retrieval (CE is local ONNX) | Markdown + SQLite |
| Honcho 3 | Agentic tool-use | 200ms, agent-directed | Dreamer (agentic specialists) | Per-query + per-dream | PostgreSQL + pgvector |
| Hindsight | 4-network architecture | Not published | Dual consolidation networks | LLM per update (supports Ollama) | Cloud or local |
| Zep (Graphiti) | Temporal KG | 2.5-3.2s, graph traversal | Real-time streaming | Optional reranker | Neo4j |
| Mem0 v1.0.2 | Hybrid vector + graph | 148ms-1.4s | Per-write updates | LLM per update (supports Ollama + FastEmbed) | Cloud or local |
| Cognee | Graph + vector pipeline | Not published | On-demand `cognify()` | LLM per extraction | Neo4j / NetworkX |
| Letta V1 | Tiered self-managed | Inline or async | Agent-driven paging | LLM per tool call | Filesystem/DB |
| Google AOMA | SQLite agent | Not published | 30-min batch consolidation | LLM per consolidation | SQLite, zero vector DB |
| MemOS 2.0 | OpenClaw plugin | Hybrid FTS5 + vector | Not published | LLM per extraction | SQLite + hybrid search |

### Strengths

- **Zero LLM cost at retrieval.** BM25/vector search + local ONNX cross-encoder. The cross-encoder (MiniLM-L-6-v2, 22.7M params) outperforms BERT-large (340M params) at 18x the speed.
- **Minimal injection.** ~149 input units per session vs 1.6k-7k tokens for competitors. Context rot research shows LLMs degrade with as few as 100 noise tokens.
- **No infrastructure.** Markdown files in a git repo. No PostgreSQL, no Neo4j, no Docker.
- **Three injection points.** Session start, per-prompt, per-file-read. Each hook has its own relevance gate.
- **Consolidation with human oversight.** Inception caps certainty at 3 so pattern notes are subject to decay. Users promote the good ones, bad ones fade.

### Weaknesses

- **No temporal graph queries.** PPR traverses the wikilink graph for structural importance, but can't answer "what changed about X between March and now?" the way Zep's temporal knowledge graph can.
- **No real-time processing.** Inception is batch (runs post-session). Patterns are always at least one session behind.
- **Benchmarking gap.** LongMemEval NDCG@10 = 0.892 measures retrieval only. Honcho reports 92.6% end-to-end accuracy (retrieval + generation + judging). Direct comparison requires a full eval with LLM generation.

## Methodology

Benchmark replays real Claude Code session transcripts through the retrieval hooks. Uses actual user prompts and file reads extracted from `.claude/projects/*/` JSONL transcripts.

```
For each session transcript:
    1. Parse JSONL for user prompts and Read tool calls
    2. Clean caches (/tmp/memento-*.json)
    3. Fire vault-briefing.py with the session's cwd
    4. For each prompt + interleaved file reads:
       - Fire vault-tool-context.py for each Read
       - Fire vault-recall.py for each prompt
    5. Record: latency (ms), injected chars, injections, skips per hook
```

Source: `benchmark/replay_benchmark.py`

**Dataset:** 30 sessions, 381 prompts, 382 file reads, 16 projects. Sessions span a work monorepo, memento-vault, personal side projects, dotfiles, and infrastructure. Session sizes 1-146 actions (median: 12).

## How to run

```bash
# Replay real sessions
python3 benchmark/replay_benchmark.py --max-sessions 30 --max-per-project 2

# Quick run
python3 benchmark/replay_benchmark.py --max-sessions 10 --max-per-project 1 --quiet

# Include Inception pipeline benchmark
python3 benchmark/replay_benchmark.py --max-sessions 30 --inception

# Optuna parameter sweep (confirmed defaults are optimal)
python3 benchmark/optuna_sweep.py --dataset data/longmemeval/longmemeval_s --n-trials 200

# LongMemEval retrieval eval
python3 benchmark/longmemeval_adapter.py --dataset data/longmemeval/longmemeval_s --mode retrieval
```

### Retrieval logs

For ongoing monitoring:

```yaml
# ~/.config/memento-vault/memento.yml
retrieval_log: true
```

Logs go to `~/.config/memento-vault/retrieval.jsonl`. Analyze with `python tools/analyze-retrieval.py --since 7`.

## References

- [Honcho 3](https://blog.plasticlabs.ai/blog/Honcho-3) -- agentic retrieval, 92.6% LongMemEval
- [Hindsight (Vectorize.io)](https://vectorize.io/hindsight/) -- 91.4% LongMemEval, 4-network architecture
- [Zep/Graphiti](https://arxiv.org/abs/2501.13956) -- temporal knowledge graph
- [Mem0](https://arxiv.org/abs/2504.19413) -- scalable long-term memory, Ollama + FastEmbed
- [Google AOMA](https://github.com/GoogleCloudPlatform/generative-ai/tree/main/gemini/agents/always-on-memory-agent) -- SQLite, zero vector DB
- [MemOS 2.0](https://github.com/MemTensor/MemOS) -- hybrid FTS5+vector in SQLite
- [Generative Agents](https://arxiv.org/abs/2304.03442) -- reflection ablation study
- [A-MEM](https://arxiv.org/abs/2502.12110) -- Zettelkasten-inspired agent memory
- [CraniMem](https://arxiv.org/abs/2603.15642) -- bounded memory with gated consolidation
- [LightMem](https://arxiv.org/abs/2510.18866) -- sleep-time consolidation
- [HippoRAG 2](https://arxiv.org/abs/2502.14802) -- PPR with dual-node KG
- [Drowning in Documents](https://arxiv.org/abs/2411.11767) -- reranker degradation beyond optimal k
- [MiniLM-L-6-v2](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2) -- 22.7M params, MRR@10 39.01
- [Context Rot](https://research.trychroma.com/context-rot) -- retrieval noise degradation
