# Benchmarks

## Setup

```bash
uv pip install rank-bm25 optuna
huggingface-cli download xiaowu0162/LongMemEval --local-dir data/longmemeval
```

For cross-encoder reranking, also install:
```bash
uv pip install onnxruntime tokenizers huggingface_hub
./tools/download-reranker.sh
```

## LongMemEval baseline (retrieval-only)

Measures recall@k, MRR, NDCG@k against ground truth. No LLM calls.

```bash
# Quick test (20 questions, ~30s)
python benchmark/longmemeval_adapter.py --dataset data/longmemeval/longmemeval_s --mode retrieval --max-questions 20

# Full run (500 questions, ~10-15 min)
python benchmark/longmemeval_adapter.py --dataset data/longmemeval/longmemeval_s --mode retrieval
```

Current baseline (2026-03-22, BM25 + adaptive PRF, no reranker):
- NDCG@10: 0.892
- MRR: 0.907
- Recall@5: 0.909

## Overnight parameter sweep

Full workflow for running on a dedicated machine.

### 1. Clone and install

```bash
git clone <repo-url> memento-vault && cd memento-vault
uv venv && source .venv/bin/activate
uv pip install rank-bm25 optuna networkx numpy
```

If running with cross-encoder reranking enabled:
```bash
uv pip install onnxruntime tokenizers huggingface_hub
./tools/download-reranker.sh
```

### 2. Download the dataset

```bash
uv pip install huggingface_hub
huggingface-cli download xiaowu0162/LongMemEval --local-dir data/longmemeval
```

### 3. Verify baseline

```bash
python benchmark/longmemeval_adapter.py --dataset data/longmemeval/longmemeval_s --mode retrieval --max-questions 20
```

Should return NDCG@10 ~ 0.9 on 20 questions.

### 4. Run the overnight sweep

```bash
# Start the sweep (8 hours, checkpoint to SQLite)
python benchmark/optuna_sweep.py \
  --dataset data/longmemeval/longmemeval_s \
  --timeout 28800 \
  --db sweep.db \
  --study-name tenet-longmemeval

# Or limit by trial count instead of time
python benchmark/optuna_sweep.py \
  --dataset data/longmemeval/longmemeval_s \
  --n-trials 500 \
  --db sweep.db
```

Use `--max-questions 100` for faster trials (~6s each instead of ~30s) at the cost of noisier metrics. Good for initial exploration, then validate top configs on the full 500.

The sweep resumes automatically if interrupted. Just re-run the same command with the same `--db` path.

### 5. Review results

Results are saved to `sweep_results.json` automatically. Top-5 configs with scores.

### 6. Apply the best config

```bash
# Preview changes (no files modified)
python benchmark/apply_sweep_results.py sweep_results.json --dry-run

# Apply the best config to source files
python benchmark/apply_sweep_results.py sweep_results.json

# Or apply the 2nd-best config (if best has a latency tradeoff you don't want)
python benchmark/apply_sweep_results.py sweep_results.json --trial 1
```

This patches the defaults in `hooks/memento_utils.py` and `benchmark/longmemeval_adapter.py`.

### 7. Verify improvement

```bash
# Re-run baseline with new defaults
python benchmark/longmemeval_adapter.py --dataset data/longmemeval/longmemeval_s --mode retrieval

# Run operational benchmark to check latency impact
python benchmark/replay_benchmark.py --max-sessions 30 --max-per-project 2
```

Compare NDCG@10 against the baseline (0.892) and recall latency against the current (443ms).

### 8. Commit if happy

```bash
git diff hooks/memento_utils.py benchmark/longmemeval_adapter.py
git add -p  # stage the config changes
git commit -m "Apply sweep results: NDCG@10 X.XXX → Y.YYY"
```

## Full end-to-end eval (generation + judging)

Runs retrieval + LLM answer generation + LLM judging via codex. Requires codex quota.

```bash
# Quick test (5 questions, ~1 min)
python benchmark/longmemeval_adapter.py --dataset data/longmemeval/longmemeval_s --mode full --max-questions 5

# Full run (500 questions, ~2 hours)
python benchmark/longmemeval_adapter.py --dataset data/longmemeval/longmemeval_s --mode full --output full_results.jsonl
```

Uses turn-level granularity by default (each conversation turn is a separate document). This keeps context small (~2-5k chars) so the LLM can process it reliably.

Tier 3 features are active by default:
- Multi-hop retrieval for multi-session and temporal questions
- Recency boost for knowledge-update questions
- Chronological context formatting for temporal questions
- Type-aware generation hints

Set `LONGMEMEVAL_BACKEND=claude` to use Claude instead of codex (requires active login).

## Quick sweep (for testing the setup)

```bash
python benchmark/optuna_sweep.py \
  --dataset data/longmemeval/longmemeval_s \
  --n-trials 10 \
  --max-questions 50
```

Takes ~5 minutes. Useful for verifying the pipeline works before starting an overnight run.

## Operational benchmark (latency/volume)

Replays real Claude Code sessions through the hooks. Measures latency, injection volume, hit rate.

```bash
python benchmark/replay_benchmark.py --max-sessions 30 --max-per-project 2
```

## Scale lifecycle benchmark

Tests the full recall pipeline (BM25 + vsearch + PRF) at increasing vault sizes by cloning your real vault and scaling it with synthetic notes.

```bash
# Default: 1x, 5x, 10x, 50x your current vault
python benchmark/scale_lifecycle.py

# Custom multipliers
python benchmark/scale_lifecycle.py --multipliers 1,10,100
```

Creates temporary QMD collections for each tier, measures real search latency, cleans up after. Results saved to `benchmark/scale_lifecycle_results.json`.

Current results (2026-03-23, 609 base notes):

| Scale | Notes | BM25 avg | Vec avg | Full pipeline |
|---|---|---|---|---|
| 1x | 609 | 309ms | 314ms | 911ms |
| 5x | 3,045 | 306ms | 292ms | 883ms |
| 10x | 6,090 | 307ms | 302ms | 917ms |
| 50x | 30,450 | 284ms | 330ms | 938ms |

BM25 and vector search are flat through 30k notes. The full pipeline cost (~900ms) is dominated by subprocess overhead (3 QMD calls), not search time.

## Latency projection benchmark

Projects BM25 search latency at synthetic vault sizes without the full pipeline.

```bash
# Default: 500 to 3000 notes, step 500
python benchmark/latency_projection.py

# Custom range
python benchmark/latency_projection.py --max-notes 5000 --step 1000
```

Results saved to `benchmark/latency_projection.json`.

## E2E lifecycle benchmark

Validates the full session lifecycle chain: triage, briefing, recall, inception. Uses mock LLM with configurable delay for best/typical/worst scenarios.

```bash
# All three scenarios
python benchmark/e2e_lifecycle.py

# Single scenario
python benchmark/e2e_lifecycle.py --scenario typical
```

Runs 6 validation checks per scenario: fleeting note written, agent notes created, recall searches relevant prompts, recall skips short prompts, multi-hop gate detects temporal queries, inception triggers.

## Retrieval analysis (dogfooding)

Analyzes the retrieval log from real usage. Covers triage decisions, recall pipeline depth, multi-hop stats, inception triggers, and latency breakdowns.

```bash
# All time
python tools/analyze-retrieval.py

# Last 7 days
python tools/analyze-retrieval.py --since 7
```

Alerts when avg recall latency crosses 700ms or P95 crosses 1.5s.

## Parameter space

The sweep optimizes these parameters:

| Parameter | Range | Default | What it controls |
|-----------|-------|---------|-----------------|
| granularity | session, turn | turn | Document chunking level |
| retrieval_limit | 3-20 | 10 | Max results from BM25 |
| recall_min_score | 0.0-0.3 | 0.0 | BM25 score floor |
| recall_high_confidence | 0.3-0.8 | 0.55 | Threshold for skipping deep path |
| prf_enabled | true/false | true | Pseudo-relevance feedback |
| prf_max_terms | 2-10 | 5 | Expansion terms from PRF |
| prf_top_docs | 1-5 | 3 | Docs used for term extraction |
| reranker_top_k | 3-15 | 10 | Candidates sent to cross-encoder |
| reranker_min_score | 0.001-0.1 | 0.01 | Cross-encoder score cutoff |
