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

How memento-vault compares to other agent memory systems:

| System | Architecture | Retrieval latency | LLM cost | Background consolidation | Storage |
|---|---|---|---|---|---|
| **Memento-vault** | BM25/vector + hooks | 530ms (BM25) | Zero at retrieval; write-time only (Inception) | Yes (Inception) | Local markdown + SQLite |
| Honcho | Agentic retrieval | 200ms (fast path) | Per-query LLM | Yes (Dreamer) | PostgreSQL + pgvector |
| Zep (Graphiti) | Temporal knowledge graph | 2.5-3.2s | Optional reranker | No (real-time streaming) | Neo4j |
| Mem0 | Hybrid vector + graph | 148ms-1.4s | LLM for updates | No | Cloud or local |
| A-MEM | Zettelkasten-inspired | Not published | LLM for linking | No | In-memory |
| MemGPT/Letta | Core blocks + retrieval | Inline or async | LLM per tool call | No | Filesystem/DB |
| Cognee | Graph + vector pipeline | Not published | LLM for extraction | No | Neo4j / NetworkX |

### Memento-vault's position

**Advantages:**
- Zero LLM cost at retrieval (pure BM25/vector search, no reranking agent)
- Minimal injected tokens (~139 units/session vs 1.6k-7k for competitors)
- Background consolidation via Inception (comparable to Honcho's Dreamer, but local-first)
- No cloud dependency, no database, no API costs — local markdown files and SQLite
- LLM costs are write-time only (Inception synthesis), zero with a Codex subscription
- Three injection points (session start, per-prompt, per-file-read) — more granular than single-endpoint systems
- Research on context rot validates the "minimal, high-signal" approach: LLMs degrade with as little as 100 tokens of noise context

**Gaps:**
- No agentic retrieval (Honcho's biggest win was switching from static top-k to agent-directed search)
- No graph traversal for multi-hop queries (Zep's temporal graph improves multi-hop reasoning)
- No real-time streaming (Zep/Graphiti process events as they happen; Inception is batch)
- No graph database (Cognee, Zep use Neo4j for entity relationships)
- No reranking stage (Zep's search/rerank/construct pipeline improves precision)
- BM25 fails on conceptual queries ("how does X work") where exact terms don't appear in vault notes

References:
- [Honcho 3](https://blog.plasticlabs.ai/blog/Honcho-3) — agentic retrieval benchmarks
- [Zep/Graphiti](https://arxiv.org/abs/2501.13956) — temporal knowledge graph
- [Mem0](https://arxiv.org/abs/2504.19413) — scalable long-term memory
- [A-MEM](https://arxiv.org/abs/2502.12110) — Zettelkasten-inspired agent memory
- [Maximum Effective Context Window](https://arxiv.org/abs/2509.21361) — context rot research
- [JetBrains: Efficient Context Management](https://blog.jetbrains.com/research/2025/12/efficient-context-management/) — agent context budgets

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
