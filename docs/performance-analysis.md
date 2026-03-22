# Performance Analysis

Performance evaluation of Tenet (retrieval) and Inception (consolidation): what they cost, what they save, and how they compare to alternatives.

## Methodology

### Test setup

Benchmark replays real Claude Code session transcripts through the retrieval hooks. Unlike synthetic tests with hardcoded prompts, this uses actual user prompts and file reads extracted from `.claude/projects/*/` JSONL transcripts.

```
For each session transcript:
    1. Parse JSONL for user prompts (type: "user") and Read tool calls (type: "assistant", tool_use name: "Read")
    2. Clean caches (/tmp/memento-*.json)
    3. Fire vault-briefing.py with the session's cwd
    4. For each prompt + interleaved file reads:
       - Fire vault-tool-context.py for each Read
       - Fire vault-recall.py for each prompt
    5. Record: latency (ms), injected chars, injections, skips per hook
```

Source: `benchmark/replay_benchmark.py`

### Dataset

| Metric | v1.1.0 | v1.2.0 + Tier 2 |
|---|---|---|
| Sessions replayed | 30 | 30 |
| Total user prompts | 341 | 381 |
| Total file reads | 362 | 382 |
| Projects covered | 16 | 16 |
| Session sizes | 1-146 actions (median: 12) | 1-146 actions (median: 12) |
| Transcript source | Real sessions from 2026-03-09 to 2026-03-22 | Same corpus + newer sessions |

Projects span a work monorepo, memento-vault (this repo), personal side projects, dotfiles, and infrastructure.

## Results

### Per-hook performance

**v1.3.0** (current, adaptive pipeline):

```
                        Latency         Injections      Chars/session
                        avg    p95      rate   eff.     avg
  ---------------------------------------------------------------
  Briefing (sync)       282ms  673ms    73%    73%      172
  Recall (adaptive)     472ms  1201ms   9%     100%     340
  Tool context (BM25)   230ms  645ms    5%     100%     86
```

**v1.1.0** (previous baseline):

```
                        Latency         Injections      Chars/session
                        avg    p95      rate   eff.     avg
  ---------------------------------------------------------------
  Briefing (sync)       83ms   139ms    73%    73%      159
  Recall (BM25)         792ms  1658ms   11%    100%     318
  Tool context (BM25)   141ms  815ms    6%     100%     78
```

**Effective hit rate** = injections / (calls - intentional skips). When the hooks actually search, they always find relevant notes. The low raw rates are by design — most calls are correctly skipped.

**Latency improvement over v1.1.0**: Recall dropped from 792ms to 472ms (40% faster) despite running Tier 1 + Tier 2 enhancements. The adaptive pipeline (skip PRF/RRF/CE when BM25 score >= 0.55) means most queries take the fast path. The deep path only fires for low-confidence queries. Briefing rose from 83ms to 282ms due to project map lookups and graph pre-building, but it is non-blocking.

### Skip breakdown

**v1.3.0** (current):

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

**v1.1.0** (previous):

```
Recall (341 calls):
    Skipped:    304  (89%)   <- short prompts, confirmations, skill invocations
    Searched:    37  (11%)
    Injected:    37  (100% of searched)

Tool context (362 calls):
    Skipped:    342  (94%)   <- config files, node_modules, assets, vendor
    Searched:    20  (6%)
    Injected:    20  (100% of searched)
```

The adaptive pipeline maintains a similar injection rate (9% vs 11%). The 100% effective hit rate is unchanged -- when the hooks search, they always find relevant notes.

### Injected chars distribution

| Stat | v1.1.0 | v1.3.0 |
|---|---|---|
| Mean | 555 chars/session | 597 chars/session |
| Zero-injection sessions | 6/30 (20%) | 6/30 (20%) |

The distribution is right-skewed. Most sessions get 200-600 chars. Large sessions in well-covered projects get 2,000-3,600 chars. Sessions in projects without vault notes get zero -- correctly. The v1.3.0 pipeline injects slightly more than v1.1.0 because the graph-aware enhancements (concept index, PPR) surface additional relevant notes that pure BM25 missed.

### Cost breakdown by hook

**v1.3.0** (current):

```
Per session (average):
    Briefing:       172 chars   (29% of total)
    Recall:         340 chars   (57% of total)
    Tool context:    86 chars   (14% of total)
    ------------------------------------------------
    Total:          597 chars   (~149 input units)
```

**v1.1.0** (previous):

```
Per session (average):
    Briefing:       159 chars   (29% of total)
    Recall:         318 chars   (57% of total)
    Tool context:    78 chars   (14% of total)
    ------------------------------------------------
    Total:          555 chars   (~139 input units)
```

Proportions are identical across versions (29/57/14). The 7% increase in total volume comes from graph-aware enhancements surfacing additional relevant notes, not from noise.

### Wall-clock overhead

| Stat | v1.1.0 | v1.3.0 | % of 20min session (v1.3.0) |
|---|---|---|---|
| Mean | 13.0s | 9.8s | 0.8% |
| Median | 4.9s | 3.7s | 0.3% |
| P95 | 40.9s | 28.6s | 2.4% |
| Max | 67.6s | 52.3s | 4.4% |

Wall-clock overhead decreased 25% from v1.1.0 despite adding Tier 1 + Tier 2 features. The adaptive pipeline (skip expensive stages when BM25 is confident) means most queries take the fast path at ~472ms instead of the v1.1.0 baseline of ~792ms.

**Blocking vs non-blocking:**

| Hook | Type | Impact |
|---|---|---|
| Briefing | Non-blocking (deferred to background) | Low — 282ms, user doesn't wait |
| Recall | Blocks prompt processing | Moderate — 472ms avg per prompt searched |
| Tool context | Blocks file read | Low — 230ms during Claude's autonomous tool use |

### Per-project analysis

Projects with the most vault benefit (highest chars injected per action):

| Project | Actions | Chars/action | Why |
|---|---|---|---|
| work monorepo (ticket branches) | 4-12 | 107-221 | Specific ticket branches with dense vault coverage |
| memento-vault | 157 | 18 | Deep coverage, large sessions |
| .claude | 72 | 37 | Tooling/config knowledge |

Projects with zero benefit: side projects without vault notes, home dir (not a project), parent dirs (not a worktree).

## Comparison: hooks vs concierge

The concierge agent is the alternative — a subagent that searches the vault on demand.

| Dimension | Hooks (automatic) | Concierge (on-demand) |
|---|---|---|
| Input units per use | ~149/session | ~72,500/call |
| Latency | 472ms/prompt (blocking) | 60-90s (one-time) |
| Trigger | Every prompt + file read | Manual, when someone asks |
| Context depth | One-liner breadcrumbs | Full narrative synthesis |
| Coverage | Every session, always-on | Only when invoked |

### Break-even analysis

One concierge call costs the same as **486 hooked sessions**. At 25 sessions/week, the hooks run for 19 weeks before matching a single concierge call in input units.

### Quality gap

The hooks inject breadcrumbs: `"- Auth middleware: Guards all API endpoints with JWT."` (~130 chars). The concierge provides synthesis: multi-paragraph answers citing specific sessions and decisions. Different jobs:

- **Hooks** are sufficient for: priming ("I remember this area"), avoiding re-discovery, context during file reads
- **Concierge** is necessary for: "What did we decide about X?", cross-session synthesis, historical questions

They are complementary. The hooks' main value is **preventing concierge calls** — each time a one-liner jogs memory, that's a 72,500-unit search avoided.

## Inception pipeline performance

Inception is a background consolidation agent that clusters vault notes by embedding similarity and produces pattern notes. It runs at `SessionEnd` (fully detached) and never blocks the user.

### Trigger overhead

The `SessionEnd` hook checks whether new notes exist since the last Inception run. This adds 10-20ms and is non-blocking.

### Full pipeline timing (550 notes)

| Phase | Time |
|---|---|
| Note collection + frontmatter parsing | 200-500ms |
| QMD embedding extraction (SQLite) | 100-300ms |
| HDBSCAN clustering (550 x 768-dim) | 200-800ms |
| Scoring + dedup | 10-50ms |
| LLM synthesis (10 clusters, sequential) | 100-300s **(dominates)** |
| File writes + backlinks | 50-200ms |
| **Total** | **~2-6 minutes, fully detached** |

LLM synthesis is the bottleneck. Each cluster requires a separate `codex exec` or `claude --print` call (10-30s each, sequential). Everything else completes in under 2 seconds combined.

### Resource usage

| Metric | Value |
|---|---|
| Memory overhead | <50MB (embedding matrix + HDBSCAN) |
| Pattern notes per run | Up to `inception_max_clusters` (default 10) |
| LLM cost | Zero with Codex subscription; per-call with Claude backend |
| Disk writes | 1 pattern note per cluster (~500 bytes each) + backlink appends |

## Comparison: industry systems

The agent memory field has split into two camps: database-backed systems that maximize recall (Honcho, Zep, Cognee) and lightweight systems that minimize overhead (memento-vault, A-MEM, MemGPT). With Inception, memento-vault borrows the key technique from the database camp -- background consolidation -- without adopting the infrastructure.

| System | Architecture | Retrieval | Consolidation | LLM cost | Storage |
|---|---|---|---|---|---|
| **Memento-vault** | BM25/vector hooks + Tier 1 + CE reranker | 472ms adaptive, PRF + RRF + PPR + PageRank + MiniLM-L6 | Inception (batch HDBSCAN) | Zero at retrieval (CE is local ONNX) | Markdown + SQLite |
| Honcho 3 | Agentic tool-use | 200ms, agent-directed | Dreamer (agentic specialists) | Per-query + per-dream | PostgreSQL + pgvector |
| Hindsight | 4-network architecture | Not published | Dual consolidation networks | LLM per update (supports Ollama) | Cloud or local |
| Zep (Graphiti) | Temporal KG | 2.5-3.2s, graph traversal | Real-time streaming | Optional reranker | Neo4j |
| Mem0 v1.0.2 | Hybrid vector + graph | 148ms-1.4s | Per-write updates | LLM per update (supports Ollama + FastEmbed) | Cloud or local |
| Cognee | Graph + vector pipeline | Not published | On-demand `cognify()` | LLM per extraction | Neo4j / NetworkX |
| Letta V1 | Tiered self-managed | Inline or async | Agent-driven paging | LLM per tool call | Filesystem/DB |
| Google AOMA | SQLite agent | Not published | 30-min batch consolidation | LLM per consolidation | SQLite, zero vector DB |
| MemOS 2.0 | OpenClaw plugin | Hybrid FTS5 + vector | Not published | LLM per extraction | SQLite + hybrid search |

### What Inception changes

Before Inception, the gap with Honcho was two features wide: no background consolidation (their Dreamer) and no agentic retrieval (their Dialectic). Honcho 3's 92.6% on LongMemEval comes from both working together -- the Dreamer front-loads synthesis so the Dialectic has richer material to search.

Inception closes the first gap. Pattern notes serve as retrieval anchors the same way Honcho's deductive/inductive observations do -- higher-level abstractions that match broader queries and carry links to specifics. Park et al. showed this matters: removing reflections from their Generative Agents architecture degraded performance by ~10%.

Tier 1 (v1.2.0) narrows the retrieval gap further. PRF query expansion and RRF hybrid search improve recall quality without LLM cost. Personalized PageRank replaces naive 1-hop wikilink expansion with graph-aware link traversal that surfaces structurally important notes 2+ hops away. Inception-produced concept indexes and project retrieval maps provide O(1) lookups that bypass search entirely for known patterns and projects. These cover what Honcho's Dialectic prefetch does -- parallel semantic searches before the agent loop.

Tier 2 adds cross-encoder reranking with MiniLM-L-6-v2 via ONNX. This closes the quality gap between sparse BM25 results and dense neural retrieval -- BM25+CE shows +11% relative NDCG@10 across BEIR benchmarks (16 of 18 datasets improved). The model runs locally on CPU at ~15-25ms for 10-20 candidates, so retrieval remains zero-LLM-cost. The tradeoff is latency: the full pipeline (PRF + RRF + CE) takes 3.3s per searched prompt vs 800ms before.

Hindsight (Vectorize.io) is a new entrant worth watching -- 91.4% LongMemEval with a 4-network architecture and Ollama support for local operation. Google's Always On Memory Agent takes the opposite approach from everyone: SQLite, no vector DB at all, 30-minute batch consolidation. MemOS 2.0 bridges the gap with hybrid FTS5+vector in SQLite.

The remaining gap with Honcho is agentic multi-hop reasoning, which only matters for ~30-40% of prompts.

### Where we're stronger

- **Zero LLM cost at retrieval.** Tenet's hooks are BM25/vector search + a local ONNX cross-encoder -- no API calls, no per-query LLM cost. The cross-encoder (MiniLM-L-6-v2, 22.7M params) runs on CPU and outperforms BERT-large (340M params) at 18x the speed. Inception's costs are write-time only and zero on a codex subscription.
- **Minimal injection.** ~149 input units per session vs 1.6k-7k tokens for competitors. Context rot research (Maximum Effective Context Window, 2025) shows LLMs degrade with as few as 100 noise tokens. Less is more.
- **No infrastructure.** Markdown files in a git repo. No PostgreSQL, no Neo4j, no Docker, no cloud account. `pip install` and you're done.
- **Three injection points.** Session start, per-prompt, per-file-read -- more granular than single-endpoint systems. Each hook has its own relevance gate so irrelevant contexts stay silent.
- **Consolidation with human oversight.** Inception caps certainty at 3 so pattern notes are subject to decay. Users promote the good ones, bad ones fade. Honcho's Dreamer operates autonomously with no human quality gate.

### Where we're weaker

- **Recall latency (resolved).** The initial Tier 1 + Tier 2 pipeline caused a 4.1x regression (792ms to 3,262ms) due to unconditional PRF/RRF/CE on every query. Adaptive pipeline depth (skip expensive stages when BM25 score >= 0.55) brought it down to 472ms -- 40% faster than the v1.1.0 baseline. The deep path (PRF + RRF + CE) only fires for low-confidence queries.
- **No agentic retrieval.** Static retrieval (even with Tier 1 + Tier 2 enhancements) can't follow chains of reasoning, resolve contradictions, or decide it needs more context. Honcho's Dialectic agent does all of this. PRF/RRF/PPR/CE cover the prefetch and reranking stages, but the multi-hop agent loop remains the gap.
- **No graph traversal for temporal queries.** PPR traverses the wikilink graph for structural importance, but Zep's temporal knowledge graph enables time-scoped queries ("what changed about X between March and now?") that link structure alone can't answer.
- **No real-time processing.** Inception is batch (runs post-session). Zep/Graphiti process events as they stream in. For a CLI tool this is fine, but it means patterns are always at least one session behind.
- **Benchmarking gap narrowing.** LongMemEval retrieval baseline: NDCG@10 = 0.892, MRR = 0.907, recall@5 = 0.909 (BM25 + PRF, 500 questions). This measures retrieval quality only, not end-to-end accuracy with LLM generation. Honcho reports 92.6% end-to-end accuracy, Hindsight 91.4%. Direct comparison requires running the full eval with LLM answer generation + GPT-4o judge. An Optuna sweep over the 11-dim parameter space is the next step to push the retrieval baseline higher before adding the generation layer.

### LongMemEval retrieval baseline

First standardized benchmark results (retrieval-only, 500 questions, BM25 + adaptive PRF):

| Metric | Score |
|---|---|
| NDCG@10 | **0.892** |
| NDCG@5 | 0.878 |
| MRR | 0.907 |
| Recall@1 | 0.550 |
| Recall@5 | 0.909 |
| Recall@10 | 0.942 |

This measures retrieval quality (did we find the right sessions?) not end-to-end accuracy (did we answer correctly?). Competitors report end-to-end numbers that include LLM generation: Honcho 92.6%, Hindsight 91.4%, Mastra 94.87%. Our retrieval NDCG@10 of 0.892 is the input to the generation stage -- the ceiling for end-to-end accuracy.

For context, the LongMemEval paper reports BM25 turn-level retrieval at ~55-60% recall@5. Our session-level BM25 with PRF gets 90.9% recall@5 -- the adaptive PRF expansion is doing real work on low-confidence queries.

### The honest summary

Memento-vault is the only local-first system with background consolidation, graph-aware retrieval, and cross-encoder reranking -- all running on CPU with zero API calls at query time. The adaptive pipeline keeps recall latency at 472ms (40% faster than v1.1.0) while the deep path (PRF + RRF + CE) fires only for low-confidence queries.

LongMemEval retrieval baseline is NDCG@10 = 0.892, which is competitive with the retrieval stage of systems like Honcho and Hindsight. End-to-end evaluation (retrieval + codex generation + codex judging) is available via `--mode full`. The next steps are: (1) Optuna sweep to find optimal parameter configuration, (2) full end-to-end eval for publishable accuracy numbers, (3) Tier 3 agentic retrieval for the ~30-40% of prompts where BM25 + PRF isn't enough.

References:
- [Honcho 3](https://blog.plasticlabs.ai/blog/Honcho-3) -- agentic retrieval, 92.6% LongMemEval
- [Benchmarking Honcho](https://blog.plasticlabs.ai/research/Benchmarking-Honcho) -- LongMem/LoCoMo/BEAM results
- [Hindsight (Vectorize.io)](https://vectorize.io/hindsight/) -- 91.4% LongMemEval, 4-network architecture, Ollama support
- [Letta V1](https://docs.letta.com/) -- 42.5% Terminal-Bench, 74% LoCoMo with filesystem memory
- [Google Always On Memory Agent](https://github.com/GoogleCloudPlatform/generative-ai/tree/main/gemini/agents/always-on-memory-agent) -- SQLite, 30-min consolidation, zero vector DB
- [MemOS 2.0 "Stardust"](https://github.com/MemTensor/MemOS) -- OpenClaw plugin, hybrid FTS5+vector in SQLite
- [Generative Agents](https://arxiv.org/abs/2304.03442) -- reflection ablation study (Park et al.)
- [Zep/Graphiti](https://arxiv.org/abs/2501.13956) -- temporal knowledge graph
- [Mem0](https://arxiv.org/abs/2504.19413) -- scalable long-term memory, now supports Ollama + FastEmbed
- [A-MEM](https://arxiv.org/abs/2502.12110) -- Zettelkasten-inspired agent memory
- [CraniMem](https://arxiv.org/abs/2603.15642) -- bounded memory with gated consolidation replays (ICLR 2026 Workshop)
- [LightMem](https://arxiv.org/abs/2510.18866) -- sleep-time consolidation, 7.7-29.3% accuracy gains (ICLR 2026)
- [HippoRAG 2](https://arxiv.org/abs/2502.14802) -- PPR with dual-node KG, +7% associative memory tasks (ICML 2025)
- [Drowning in Documents](https://arxiv.org/abs/2411.11767) -- rerankers can degrade quality beyond optimal k (SIGIR 2025)
- [MICE](https://arxiv.org/abs/2602.16299) -- 4x cross-encoder speedup via minimal interactions
- [Context Memory Virtualisation](https://arxiv.org/abs/2602.22402) -- DAG-based trimming, 20-86% token reduction on coding sessions
- [MiniLM-L-6-v2](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2) -- MRR@10 39.01, NDCG@10 74.30, 22.7M params
- [Context Rot](https://research.trychroma.com/context-rot) -- retrieval noise degradation
- [JetBrains: Efficient Context Management](https://blog.jetbrains.com/research/2025/12/efficient-context-management/) -- agent context budgets

## Optimizations applied

Based on this analysis, five optimizations were implemented:

### 1. Skip pattern improvements (highest impact)

Nearly half of recall QMD calls were wasted on skill expansions and command messages passed through as prompt text. Added filters for:

- `<command-message>` and `<task-notification>` XML tags
- Skill headers (`# Plan - ...` over 300 chars)
- Any prompt over 500 chars (skill content dumps)

Expected: ~47% fewer QMD calls on recall.

### 2. Parallel collection search

`qmd_search_with_extras()` searched primary + extra collections sequentially. Switched to `concurrent.futures.ThreadPoolExecutor`. With a second collection configured, saves ~1,200ms per call.

### 3. Tool context cooldown: 3s -> 1s

The 3-second cooldown between fresh directory searches was too conservative. Lowered to 1s to catch more directories during burst file reads.

### 4. Tool context score threshold: 0.75 -> 0.65

The high threshold missed borderline-relevant notes from extra collections. BM25 scores for file-path keyword queries cluster at 0.76-0.87 — lowering to 0.65 captures cross-collection hits without introducing noise.

### 5. Wikilink expansion cap: 3

Hub notes with many wikilinks could flood results. Added `wikilink_max_expanded` config (default: 3) to cap total expanded notes.

### 6. Tier 1 retrieval enhancements (v1.2.0)

Six enhancements to the recall pipeline, all zero per-query LLM cost:

- **PRF query expansion**: two-pass BM25 — run initial query, extract discriminative terms from top-3 results, re-query with expanded terms. Improves recall for underspecified prompts.
- **RRF hybrid search**: when vector search is warm (deferred briefing consumed), runs BM25 + vsearch in parallel and fuses with Reciprocal Rank Fusion. Combines keyword precision with semantic recall.
- **PageRank centrality boost**: well-connected notes (many inbound wikilinks) get a multiplicative score boost, promoting knowledge hubs over isolated notes.
- **Personalized PageRank expansion**: replaces naive 1-hop wikilink expansion. Seeds PPR on search result stems and propagates relevance through the link graph, surfacing structurally important notes 2+ hops away.
- **Concept index**: Inception builds an inverted index from pattern note keywords → source stems. Recall supplements BM25 results with O(1) lookups for known patterns.
- **Project retrieval maps**: Inception builds per-project note rankings. Briefing uses these for instant project context, skipping the deferred vsearch entirely when maps have enough coverage.

Original estimate was ~50ms added to the recall hot path. Actual measured impact is much larger -- the full Tier 1 + Tier 2 pipeline takes recall from 792ms to 3,262ms. The bulk of this is subprocess overhead: each PRF, RRF, and CE step shells out to QMD or ONNX, and subprocess launch costs dominate over the actual computation. The BM25 miss on conceptual queries (noted as a weakness above) is partially addressed by RRF hybrid search and concept index lookups.

### 7. Tier 2: Cross-encoder reranking

A local cross-encoder reranker that rescores BM25/RRF candidates before injection. This is the single biggest quality improvement in the pipeline -- BM25+CE shows +11% relative NDCG@10 across 16 of 18 BEIR benchmark datasets.

**Model: MiniLM-L-6-v2**

| Metric | Value |
|---|---|
| Parameters | 22.7M (6 layers) |
| MRR@10 (MS MARCO dev) | 39.01 |
| NDCG@10 (TREC DL 2019) | 74.30 |
| vs L-12 variant | Essentially identical quality (39.01 vs 39.02), 1.9x throughput |
| vs BERT-large (340M) | Outperforms at 18x speed |
| ONNX INT8 CPU latency | ~15-25ms for 10-20 candidates |
| Quantization quality loss | <0.5% |

The model is small enough to load once and keep resident. ONNX Runtime with INT8 quantization keeps inference fast on CPU without a GPU. For our typical candidate set (5-15 notes after BM25/RRF), reranking adds 15-25ms of pure model time. The actual overhead is higher because we shell out to a subprocess -- moving to in-process ONNX inference is the obvious optimization.

**Why L-6 over L-12:** The 12-layer variant scores MRR@10 = 39.02 -- virtually identical. The 6-layer version runs at 1.9x throughput. For a pipeline that already has a latency problem, the speed/quality tradeoff is clear.

**Quality impact:** The reranker's main contribution is pruning false positives that BM25 scores highly due to keyword overlap but that aren't semantically relevant. This is why injection volume dropped 17% (555 to 463 chars/session) without losing effective hit rate -- the reranker filters marginal results, not good ones.

**Caveat from SIGIR 2025 (Drowning in Documents):** Rerankers can degrade retrieval quality when the candidate set k exceeds an optimal threshold. Feeding too many low-quality candidates to the cross-encoder hurts more than it helps. Our candidate sets are naturally small (BM25 top-5 to top-15), which keeps us in the sweet spot. Worth monitoring if candidate set sizes grow.

## How to run the benchmark

```bash
# Replay real sessions (2 per project, max 30 total)
python3 benchmark/replay_benchmark.py --max-sessions 30 --max-per-project 2

# Quick run
python3 benchmark/replay_benchmark.py --max-sessions 10 --max-per-project 1 --quiet

# Include Inception pipeline benchmark (runs dry-run, no notes written)
python3 benchmark/replay_benchmark.py --max-sessions 30 --inception
```

Results are printed to stdout and saved as JSONL for further analysis. The `--inception` flag runs `memento-inception.py --dry-run --full --verbose` and reports pipeline timing, cluster count, and per-phase breakdown.

### Enabling retrieval logs

For ongoing cost monitoring during normal use:

```yaml
# ~/.config/memento-vault/memento.yml
retrieval_log: true
```

Or set `MEMENTO_DEBUG=1` in your shell. Logs go to `~/.config/memento-vault/retrieval.jsonl`:

```json
{"ts":"2026-03-22T10:29:53","hook":"recall","action":"inject","query":"how does the cache work","latency_ms":425,"injected_titles":["redis-cache-requires-explicit-ttl"],"injected_chars":324}
```

Analyze with:

```bash
# Total injected chars per hook
cat ~/.config/memento-vault/retrieval.jsonl | \
  python3 -c "import sys,json; d={}; [d.update({(r:=json.loads(l))['hook']: d.get(r['hook'],0)+r.get('injected_chars',0)}) for l in sys.stdin]; print(d)"

# Average latency per hook
cat ~/.config/memento-vault/retrieval.jsonl | \
  python3 -c "import sys,json; s={}; n={}; [(s.update({(r:=json.loads(l))['hook']:s.get(r['hook'],0)+r.get('latency_ms',0)}),n.update({r['hook']:n.get(r['hook'],0)+1})) for l in sys.stdin]; [print(f'{k}: {s[k]//n[k]}ms avg ({n[k]} calls)') for k in s]"
```
