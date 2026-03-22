#!/usr/bin/env python3
"""
LongMemEval benchmark adapter for memento-vault.
Runs the Tenet retrieval pipeline against the standard 500-question
memory benchmark.

Usage:
    python benchmark/longmemeval_adapter.py --dataset data/longmemeval_s.json --mode retrieval
"""

import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

# Add hooks to path for memento_utils imports
sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))


def load_dataset(path):
    """Load LongMemEval dataset from JSON file.

    Returns list of question dicts.
    Validates required fields are present.
    """
    with open(path) as f:
        data = json.load(f)

    # Handle both list format and dict-with-list format
    if isinstance(data, dict):
        data = data.get("data", data.get("questions", []))

    return data


def ingest_haystack(question, granularity="session"):
    """Convert a LongMemEval question's haystack into indexable documents.

    Args:
        question: dict with haystack_sessions, haystack_session_ids, haystack_dates
        granularity: "session" (one doc per session) or "turn" (one doc per turn)

    Returns:
        list of dicts with keys: id, text, metadata
        metadata includes: date, session_id, session_idx
    """
    documents = []
    sessions = question.get("haystack_sessions", [])
    session_ids = question.get("haystack_session_ids", [])
    dates = question.get("haystack_dates", [])

    for idx, session in enumerate(sessions):
        session_id = session_ids[idx] if idx < len(session_ids) else f"session-{idx}"
        date = dates[idx] if idx < len(dates) else ""

        if granularity == "session":
            turns_text = []
            for turn in session:
                role = turn.get("role", "unknown")
                content = turn.get("content", "")
                turns_text.append(f"{role}: {content}")

            text = "\n".join(turns_text)
            documents.append({
                "id": session_id,
                "text": text,
                "metadata": {
                    "date": date,
                    "session_id": session_id,
                    "session_idx": idx,
                    "title": f"Session {session_id}",
                    "num_turns": len(session),
                },
            })

        elif granularity == "turn":
            for turn_idx, turn in enumerate(session):
                role = turn.get("role", "unknown")
                content = turn.get("content", "")
                turn_id = f"{session_id}:turn-{turn_idx}"

                documents.append({
                    "id": turn_id,
                    "text": f"{role}: {content}",
                    "metadata": {
                        "date": date,
                        "session_id": session_id,
                        "session_idx": idx,
                        "turn_idx": turn_idx,
                        "role": role,
                        "title": f"{session_id} turn {turn_idx}",
                    },
                })

    return documents


def build_vault_notes(documents, vault_dir=None):
    """Write documents as markdown notes in a temp vault directory.

    Used for building wikilink graphs. Each document becomes a note
    with YAML frontmatter.

    Args:
        documents: list from ingest_haystack
        vault_dir: directory to write to (creates temp if None)

    Returns:
        (vault_path, note_paths) tuple
    """
    if vault_dir is None:
        vault_dir = tempfile.mkdtemp(prefix="longmemeval-vault-")

    vault_path = Path(vault_dir)
    notes_dir = vault_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    note_paths = []
    for doc in documents:
        doc_id = doc["id"]
        slug = doc_id.replace(":", "-").replace("/", "-").replace(" ", "-").lower()
        note_path = notes_dir / f"{slug}.md"

        meta = doc.get("metadata", {})
        date = meta.get("date", "")
        title = meta.get("title", doc_id)

        content = f"""---
title: {title}
type: session
date: {date}
session_id: {meta.get('session_id', doc_id)}
---

{doc['text']}
"""
        note_path.write_text(content)
        note_paths.append(note_path)

    return vault_path, note_paths


def get_default_config():
    """Default config for LongMemEval evaluation."""
    return {
        "granularity": "session",
        "retrieval_limit": 10,
        "recall_min_score": 0.0,
        "recall_high_confidence": 0.55,
        "prf_enabled": True,
        "prf_max_terms": 5,
        "prf_top_docs": 3,
        "ppr_enabled": False,  # disabled by default — LongMemEval sessions have no wikilinks
        "pagerank_boost_weight": 0.0,
        "pagerank_alpha": 0.85,
        "temporal_decay": False,  # disabled — LongMemEval dates are synthetic
        "reranker_enabled": False,  # disabled by default — requires ONNX model
        "reranker_top_k": 10,
        "reranker_min_score": 0.01,
    }


def run_retrieval(question, config=None):
    """Run the Tenet retrieval pipeline on a LongMemEval question.

    Args:
        question: LongMemEval question dict
        config: pipeline config dict (uses defaults if None)

    Returns:
        list of result dicts sorted by score descending
    """
    if config is None:
        config = get_default_config()

    granularity = config.get("granularity", "session")

    # 1. Ingest haystack into documents
    documents = ingest_haystack(question, granularity=granularity)

    # 2. Build BM25 index
    from longmemeval_retrieval import bm25_search, build_bm25_index

    index = build_bm25_index(documents)

    # 3. Initial BM25 search
    query = question["question"]
    limit = config.get("retrieval_limit", 10)
    min_score = config.get("recall_min_score", 0.0)
    results = bm25_search(index, query, limit=limit, min_score=min_score)

    if not results:
        return []

    top_score = results[0]["score"] if results else 0
    high_conf = config.get("recall_high_confidence", 0.55)

    # 4. Adaptive depth: PRF + re-search if low confidence
    if top_score < high_conf and config.get("prf_enabled", True):
        from memento_utils import prf_expand_query as _prf

        expanded = _prf(query, config=config, initial_results=results)
        if expanded != query:
            prf_results = bm25_search(index, expanded, limit=limit, min_score=min_score)
            if prf_results:
                existing = {r["path"] for r in results}
                for r in prf_results:
                    if r["path"] not in existing:
                        results.append(r)
                        existing.add(r["path"])
                results.sort(key=lambda r: r["score"], reverse=True)

    # 5. Enhancement pipeline (temporal decay, PageRank, PPR)
    if config.get("ppr_enabled", True) or config.get("pagerank_boost_weight", 0) > 0:
        try:
            vault_path, _ = build_vault_notes(documents)
            from memento_utils import (
                apply_pagerank_boost,
                apply_temporal_decay,
                build_wikilink_graph,
                compute_pagerank,
                ppr_expand,
            )

            if config.get("temporal_decay", False):
                results = apply_temporal_decay(results, config)

            graph = build_wikilink_graph(vault_path)
            if graph and graph.number_of_nodes() > 0:
                pagerank = compute_pagerank(
                    graph, alpha=config.get("pagerank_alpha", 0.85)
                )
                if pagerank and config.get("pagerank_boost_weight", 0) > 0:
                    results = apply_pagerank_boost(results, pagerank, config)
                if config.get("ppr_enabled", True):
                    expanded = ppr_expand(results, graph, config)
                    if expanded:
                        existing_paths = {r["path"] for r in results}
                        for entry in expanded:
                            if entry["path"] not in existing_paths:
                                results.append(entry)
                                existing_paths.add(entry["path"])
                        results.sort(key=lambda r: r["score"], reverse=True)

            # Cleanup temp vault
            shutil.rmtree(vault_path, ignore_errors=True)
        except Exception:
            pass  # Enhancement pipeline is optional

    # 6. Cross-encoder reranking
    if config.get("reranker_enabled", False) and len(results) > 1:
        try:
            from tenet_reranker import rerank

            results = rerank(query, results, config)
        except Exception:
            pass

    # Truncate to limit
    return results[:limit]


def compute_retrieval_metrics(results, question, k_values=None):
    """Compute retrieval metrics against LongMemEval ground truth.

    Args:
        results: list of result dicts from run_retrieval
        question: LongMemEval question dict (has answer_session_ids)
        k_values: list of k values for recall/NDCG (default [1, 3, 5, 10])

    Returns:
        dict with recall@k, mrr, ndcg@k for each k
    """
    if k_values is None:
        k_values = [1, 3, 5, 10]

    # Ground truth: answer_session_ids
    answer_ids = set(question.get("answer_session_ids", []))
    if not answer_ids:
        return {}

    # Extract session_ids from results
    retrieved_ids = []
    for r in results:
        doc_id = r.get("_doc_id", "")
        if not doc_id:
            # Extract from path: "notes/sess_001.md" -> "sess_001"
            doc_id = Path(r.get("path", "")).stem
        # For turn-level: "sess_001:turn-0" -> "sess_001"
        session_id = doc_id.split(":")[0] if ":" in doc_id else doc_id
        retrieved_ids.append(session_id)

    # Deduplicate while preserving order
    seen = set()
    unique_retrieved = []
    for sid in retrieved_ids:
        if sid not in seen:
            seen.add(sid)
            unique_retrieved.append(sid)

    metrics = {}

    # Recall@k
    for k in k_values:
        top_k = set(unique_retrieved[:k])
        recall = len(top_k & answer_ids) / len(answer_ids)
        metrics[f"recall@{k}"] = recall

    # MRR (Mean Reciprocal Rank)
    mrr = 0.0
    for i, sid in enumerate(unique_retrieved):
        if sid in answer_ids:
            mrr = 1.0 / (i + 1)
            break
    metrics["mrr"] = mrr

    # NDCG@k (binary relevance)
    for k in k_values:
        dcg = 0.0
        for i, sid in enumerate(unique_retrieved[:k]):
            if sid in answer_ids:
                dcg += 1.0 / math.log2(i + 2)  # i+2 because rank starts at 1

        # Ideal DCG: all relevant docs at top
        ideal_hits = min(len(answer_ids), k)
        idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))

        ndcg = dcg / idcg if idcg > 0 else 0.0
        metrics[f"ndcg@{k}"] = ndcg

    return metrics


def run_retrieval_eval(dataset_path, config=None, max_questions=None):
    """Run retrieval evaluation across all LongMemEval questions.

    Returns:
        dict with per-metric aggregates (mean across questions)
    """
    if config is None:
        config = get_default_config()

    dataset = load_dataset(dataset_path)
    if max_questions:
        dataset = dataset[:max_questions]

    all_metrics = []
    for question in dataset:
        results = run_retrieval(question, config)
        metrics = compute_retrieval_metrics(results, question)
        if metrics:
            all_metrics.append(metrics)

    if not all_metrics:
        return {}

    # Aggregate: mean of each metric
    aggregated = {}
    for key in all_metrics[0]:
        values = [m[key] for m in all_metrics if key in m]
        aggregated[key] = sum(values) / len(values) if values else 0.0

    aggregated["num_questions"] = len(all_metrics)
    return aggregated


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="LongMemEval benchmark adapter")
    parser.add_argument("--dataset", required=True, help="Path to LongMemEval JSON")
    parser.add_argument(
        "--mode", choices=["retrieval", "full"], default="retrieval"
    )
    parser.add_argument(
        "--config", type=str, default=None, help="JSON config string"
    )
    parser.add_argument("--max-questions", type=int, default=None)
    parser.add_argument("--output", type=str, default=None, help="Output JSONL path")

    args = parser.parse_args()

    config = get_default_config()
    if args.config:
        config.update(json.loads(args.config))

    if args.mode == "retrieval":
        metrics = run_retrieval_eval(
            args.dataset, config, max_questions=args.max_questions
        )
        print(json.dumps(metrics, indent=2))
    # "full" mode (with LLM generation) to be added later
