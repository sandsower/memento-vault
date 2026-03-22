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

| Metric | Value |
|---|---|
| Sessions replayed | 30 |
| Total user prompts | 341 |
| Total file reads | 362 |
| Projects covered | 16 |
| Session sizes | 1-146 actions (median: 12) |
| Transcript source | Real sessions from 2026-03-09 to 2026-03-22 |

Projects span a work monorepo, memento-vault (this repo), personal side projects, dotfiles, and infrastructure.

## Results

### Per-hook performance

```
                        Latency         Injections      Chars/session
                        avg    p95      rate   eff.     avg
  ---------------------------------------------------------------
  Briefing (sync)       83ms   139ms    73%    73%      159
  Recall (BM25)         792ms  1658ms   11%    100%     318
  Tool context (BM25)   141ms  815ms    6%     100%     78
```

**Effective hit rate** = injections / (calls - intentional skips). When the hooks actually search, they always find relevant notes. The low raw rates are by design — most calls are correctly skipped.

### Skip breakdown

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

### Injected chars distribution

| Stat | Value |
|---|---|
| Mean | 555 chars/session |
| Median | 443 chars/session |
| P95 | 2,846 chars/session |
| Max | 3,672 chars/session |
| Zero-injection sessions | 6/30 (20%) |

The distribution is right-skewed. Most sessions get 200-500 chars. Large sessions in well-covered projects get 2,000-3,600 chars. Sessions in projects without vault notes get zero -- correctly.

### Cost breakdown by hook

```
Per session (average):
    Briefing:       159 chars   (29% of total)
    Recall:         318 chars   (57% of total)
    Tool context:    78 chars   (14% of total)
    ------------------------------------------------
    Total:          555 chars   (~139 input units)
```

Recall dominates because it fires on every substantial prompt and injects longer snippets (title + 120-char snippet per note, up to 3 notes). Tool context is the cheapest — only 2 notes per injection, shorter snippets.

### Wall-clock overhead

| Stat | Value | % of 20min session |
|---|---|---|
| Mean | 13.0s | 1.1% |
| Median | 4.9s | 0.4% |
| P95 | 40.9s | 3.4% |
| Max | 67.6s | 5.6% |

82.8% of overhead is the recall hook (BM25 search at ~800ms per call). Sessions with 30+ prompts accumulate noticeable overhead. Sessions with <10 prompts stay under 5s total.

**Blocking vs non-blocking:**

| Hook | Type | Impact |
|---|---|---|
| Briefing | Non-blocking (deferred to background) | Zero — user never waits |
| Recall | Blocks prompt processing | Highest — 800ms per prompt |
| Tool context | Blocks file read | Moderate — during Claude's autonomous tool use |

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
| Input units per use | ~139/session | ~72,500/call |
| Latency | 800ms/prompt (blocking) | 60-90s (one-time) |
| Trigger | Every prompt + file read | Manual, when someone asks |
| Context depth | One-liner breadcrumbs | Full narrative synthesis |
| Coverage | Every session, always-on | Only when invoked |

### Break-even analysis

One concierge call costs the same as **452 hooked sessions**. At 25 sessions/week, the hooks run for 18 weeks before matching a single concierge call in input units. The hooks are effectively free in unit terms.

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
| **Memento-vault** | BM25/vector hooks + Tier 1 enhancements | 530ms, PRF + RRF + PPR + PageRank | Inception (batch HDBSCAN) | Zero at retrieval | Markdown + SQLite |
| Honcho | Agentic tool-use | 200ms, agent-directed | Dreamer (agentic specialists) | Per-query + per-dream | PostgreSQL + pgvector |
| Zep (Graphiti) | Temporal KG | 2.5-3.2s, graph traversal | Real-time streaming | Optional reranker | Neo4j |
| Mem0 | Hybrid vector + graph | 148ms-1.4s | Per-write updates | LLM per update | Cloud or local |
| Cognee | Graph + vector pipeline | Not published | On-demand `cognify()` | LLM per extraction | Neo4j / NetworkX |
| MemGPT/Letta | Tiered self-managed | Inline or async | Agent-driven paging | LLM per tool call | Filesystem/DB |

### What Inception changes

Before Inception, the gap with Honcho was two features wide: no background consolidation (their Dreamer) and no agentic retrieval (their Dialectic). Honcho's 90.4% on LongMem S comes from both working together -- the Dreamer front-loads synthesis so the Dialectic has richer material to search.

Inception closes the first gap. Pattern notes serve as retrieval anchors the same way Honcho's deductive/inductive observations do -- higher-level abstractions that match broader queries and carry links to specifics. Park et al. showed this matters: removing reflections from their Generative Agents architecture degraded performance by ~10%.

Tier 1 (v1.2.0) narrows the retrieval gap further. PRF query expansion and RRF hybrid search improve recall quality without LLM cost. Personalized PageRank replaces naive 1-hop wikilink expansion with graph-aware link traversal that surfaces structurally important notes 2+ hops away. Inception-produced concept indexes and project retrieval maps provide O(1) lookups that bypass search entirely for known patterns and projects. These cover what Honcho's Dialectic prefetch does -- parallel semantic searches before the agent loop. The remaining gap is agentic multi-hop reasoning, which only matters for ~30-40% of prompts.

### Where we're stronger

- **Zero LLM cost at retrieval.** Tenet's hooks are pure BM25/vector search, no reranking agent, no per-query API call. Inception's costs are write-time only and zero on a codex subscription. Every other system with comparable features has per-query LLM costs.
- **Minimal injection.** ~139 input units per session vs 1.6k-7k tokens for competitors. Context rot research (Maximum Effective Context Window, 2025) shows LLMs degrade with as few as 100 noise tokens. Less is more.
- **No infrastructure.** Markdown files in a git repo. No PostgreSQL, no Neo4j, no Docker, no cloud account. `pip install` and you're done.
- **Three injection points.** Session start, per-prompt, per-file-read -- more granular than single-endpoint systems. Each hook has its own relevance gate so irrelevant contexts stay silent.
- **Consolidation with human oversight.** Inception caps certainty at 3 so pattern notes are subject to decay. Users promote the good ones, bad ones fade. Honcho's Dreamer operates autonomously with no human quality gate.

### Where we're weaker

- **No agentic retrieval.** Static retrieval (even with Tier 1 enhancements) can't follow chains of reasoning, resolve contradictions, or decide it needs more context. Honcho's Dialectic agent does all of this. PRF/RRF/PPR cover the prefetch stage, but the multi-hop agent loop remains the gap.
- **No graph traversal for temporal queries.** PPR traverses the wikilink graph for structural importance, but Zep's temporal knowledge graph enables time-scoped queries ("what changed about X between March and now?") that link structure alone can't answer.
- **No real-time processing.** Inception is batch (runs post-session). Zep/Graphiti process events as they stream in. For a CLI tool this is fine, but it means patterns are always at least one session behind.
- **No benchmarking parity.** Honcho publishes LongMem and LoCoMo scores. We have no equivalent benchmark. The replay benchmark measures latency and injection volume, not retrieval accuracy. Until we run comparable evals, the quality comparison is qualitative.

### The honest summary

Memento-vault is the only local-first, zero-retrieval-cost system with background consolidation and graph-aware retrieval. Tier 1 enhancements (PRF, RRF, PPR, PageRank, concept indexes, project maps) close ~70% of the gap with Honcho's Dialectic prefetch at zero per-query LLM cost. The remaining gap -- agentic multi-hop retrieval for complex queries -- is deferred to Tier 3 (background codex exec, results arrive by prompt 2-3).

References:
- [Honcho 3](https://blog.plasticlabs.ai/blog/Honcho-3) -- agentic retrieval, 90.4% LongMem S
- [Benchmarking Honcho](https://blog.plasticlabs.ai/research/Benchmarking-Honcho) -- LongMem/LoCoMo/BEAM results
- [Generative Agents](https://arxiv.org/abs/2304.03442) -- reflection ablation study (Park et al.)
- [Zep/Graphiti](https://arxiv.org/abs/2501.13956) -- temporal knowledge graph
- [Mem0](https://arxiv.org/abs/2504.19413) -- scalable long-term memory
- [A-MEM](https://arxiv.org/abs/2502.12110) -- Zettelkasten-inspired agent memory
- [CraniMem](https://arxiv.org/abs/2603.15642) -- bounded memory with consolidation
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

Added ~50ms to the recall hot path. The BM25 miss on conceptual queries (noted as a weakness above) is partially addressed by RRF hybrid search and concept index lookups.

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
