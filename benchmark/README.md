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

## Optuna overnight sweep

Tunes retrieval parameters against LongMemEval. SQLite checkpoint/resume.

```bash
# Quick sweep (10 trials, ~5 min)
python benchmark/optuna_sweep.py --dataset data/longmemeval/longmemeval_s --n-trials 10 --max-questions 50

# Overnight sweep (8 hours, ~960 trials)
python benchmark/optuna_sweep.py --dataset data/longmemeval/longmemeval_s --timeout 28800 --db sweep.db

# Resume interrupted sweep
python benchmark/optuna_sweep.py --dataset data/longmemeval/longmemeval_s --timeout 28800 --db sweep.db

# Export top configs
# (automatically saved to sweep_results.json)
```

## Operational benchmark (latency/volume)

Replays real Claude Code sessions through the hooks.

```bash
python benchmark/replay_benchmark.py --max-sessions 30 --max-per-project 2
```
