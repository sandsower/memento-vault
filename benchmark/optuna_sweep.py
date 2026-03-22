#!/usr/bin/env python3
"""
Optuna parameter sweep for Tenet retrieval pipeline.
Optimizes retrieval parameters against LongMemEval using retrieval-only
metrics (no LLM calls needed). Runs overnight with checkpoint/resume.

Usage:
    python benchmark/optuna_sweep.py --dataset data/longmemeval_s.json --timeout 28800
    python benchmark/optuna_sweep.py --dataset data/longmemeval_s.json --n-trials 200
    python benchmark/optuna_sweep.py --resume --db sweep.db  # resume interrupted run
"""

import argparse
import json
import sys
from pathlib import Path

import optuna

# Add benchmark/ to path for adapter imports
sys.path.insert(0, str(Path(__file__).parent))

from longmemeval_adapter import run_retrieval_eval


def define_search_space(trial):
    """Define the 11-dimensional parameter search space.

    Returns a config dict compatible with run_retrieval and get_default_config.
    """
    config = {
        # Granularity
        "granularity": trial.suggest_categorical("granularity", ["session", "turn"]),
        # Retrieval
        "retrieval_limit": trial.suggest_int("retrieval_limit", 3, 20),
        "recall_min_score": trial.suggest_float("recall_min_score", 0.0, 0.3),
        # Adaptive pipeline threshold
        "recall_high_confidence": trial.suggest_float("recall_high_confidence", 0.3, 0.8),
        # PRF
        "prf_enabled": trial.suggest_categorical("prf_enabled", [True, False]),
        "prf_max_terms": trial.suggest_int("prf_max_terms", 2, 10),
        "prf_top_docs": trial.suggest_int("prf_top_docs", 1, 5),
        # Enhancement pipeline
        "ppr_enabled": False,  # LongMemEval sessions have no wikilinks
        "pagerank_boost_weight": 0.0,
        "temporal_decay": False,  # LongMemEval dates are synthetic
        # Reranker (optional — only if model is available)
        "reranker_enabled": False,  # can be overridden via CLI flag
        "reranker_top_k": trial.suggest_int("reranker_top_k", 3, 15),
        "reranker_min_score": trial.suggest_float("reranker_min_score", 0.001, 0.1, log=True),
    }

    return config


def create_objective(dataset_path, max_questions=None, metric="ndcg@10"):
    """Create an Optuna objective function.

    Args:
        dataset_path: path to LongMemEval JSON
        max_questions: limit questions per trial (for speed)
        metric: which metric to optimize (default ndcg@10)

    Returns:
        callable(trial) -> float
    """
    def objective(trial):
        config = define_search_space(trial)

        # Run retrieval evaluation
        metrics = run_retrieval_eval(dataset_path, config, max_questions=max_questions)

        if not metrics or metric not in metrics:
            return 0.0

        return metrics[metric]

    return objective


def run_sweep(dataset_path, n_trials=200, timeout=None, db_path="sweep.db",
              study_name="tenet-longmemeval", max_questions=None, metric="ndcg@10"):
    """Run the Optuna parameter sweep.

    Args:
        dataset_path: path to LongMemEval JSON
        n_trials: max number of trials
        timeout: max seconds (None = no timeout)
        db_path: SQLite path for checkpoint/resume
        study_name: Optuna study name
        max_questions: limit questions per trial
        metric: optimization target

    Returns:
        optuna.Study
    """
    storage = f"sqlite:///{db_path}"

    study = optuna.create_study(
        study_name=study_name,
        storage=storage,
        load_if_exists=True,
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(
            n_startup_trials=10,
            n_warmup_steps=2,
        ),
    )

    objective = create_objective(dataset_path, max_questions=max_questions, metric=metric)

    study.optimize(
        objective,
        n_trials=n_trials,
        timeout=timeout,
        show_progress_bar=True,
    )

    return study


def export_results(study, output_path="sweep_results.json", top_n=5):
    """Export top-N trial configs from a completed study.

    Args:
        study: optuna.Study
        output_path: JSON output path
        top_n: number of top configs to export
    """
    trials = sorted(study.trials, key=lambda t: t.value if t.value is not None else -1, reverse=True)

    results = {
        "study_name": study.study_name,
        "n_trials": len(study.trials),
        "best_value": study.best_value,
        "best_params": study.best_params,
        "top_configs": [],
    }

    for trial in trials[:top_n]:
        if trial.value is not None:
            results["top_configs"].append({
                "trial_number": trial.number,
                "value": trial.value,
                "params": trial.params,
            })

    Path(output_path).write_text(json.dumps(results, indent=2))
    return results


def print_summary(study):
    """Print a summary of the sweep results."""
    print(f"\n{'='*60}")
    print(f"  SWEEP COMPLETE: {study.study_name}")
    print(f"{'='*60}")
    print(f"  Trials completed: {len(study.trials)}")
    print(f"  Best value:       {study.best_value:.4f}")
    print(f"  Best params:")
    for k, v in study.best_params.items():
        print(f"    {k}: {v}")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="Optuna parameter sweep for Tenet retrieval")
    parser.add_argument("--dataset", required=True, help="Path to LongMemEval JSON")
    parser.add_argument("--n-trials", type=int, default=200, help="Max trials")
    parser.add_argument("--timeout", type=int, default=None, help="Max seconds (default: no limit)")
    parser.add_argument("--db", type=str, default="sweep.db", help="SQLite path for checkpoint/resume")
    parser.add_argument("--study-name", type=str, default="tenet-longmemeval")
    parser.add_argument("--max-questions", type=int, default=None, help="Questions per trial (default: all)")
    parser.add_argument("--metric", type=str, default="ndcg@10", help="Optimization target")
    parser.add_argument("--export", type=str, default="sweep_results.json", help="Output path for results")
    parser.add_argument("--export-top", type=int, default=5, help="Top-N configs to export")

    args = parser.parse_args()

    print(f"Starting sweep: {args.n_trials} trials, metric={args.metric}")
    if args.timeout:
        print(f"Timeout: {args.timeout}s ({args.timeout/3600:.1f}h)")

    study = run_sweep(
        dataset_path=args.dataset,
        n_trials=args.n_trials,
        timeout=args.timeout,
        db_path=args.db,
        study_name=args.study_name,
        max_questions=args.max_questions,
        metric=args.metric,
    )

    print_summary(study)
    results = export_results(study, output_path=args.export, top_n=args.export_top)
    print(f"Results exported to {args.export}")


if __name__ == "__main__":
    main()
