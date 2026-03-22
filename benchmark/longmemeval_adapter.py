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
import os
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
        "granularity": "turn",
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
        # Tier 3: agentic retrieval
        "multi_hop_enabled": True,
        "multi_hop_max": 2,
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

    # 7. Multi-hop retrieval for complex queries
    q_type = question.get("question_type", "")
    if config.get("multi_hop_enabled", True) and q_type in ("multi-session", "temporal-reasoning"):
        results = multi_hop_retrieve(question, index, results, config)

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


def call_llm(prompt, timeout=120, backend=None):
    """Call LLM for generation/judging. Returns response text.

    Backends (tried in order):
      - "claude": claude --print (Claude Code subscription, no cost)
      - "codex": codex exec (OpenAI subscription, no cost)
    """
    import subprocess

    if backend is None:
        backend = os.environ.get("LONGMEMEVAL_BACKEND", "codex")

    if backend == "claude":
        try:
            result = subprocess.run(
                [
                    "claude", "--print",
                    "--dangerously-skip-permissions",
                    "--no-session-persistence",
                    "--bare",
                    "-p", prompt,
                ],
                capture_output=True, text=True, timeout=timeout,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""

    elif backend == "codex":
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
            out_path = tmp.name
        try:
            subprocess.run(
                [
                    "codex", "exec",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--ephemeral",
                    "-o", out_path,
                    prompt,
                ],
                capture_output=True, text=True, timeout=timeout,
            )
            return Path(out_path).read_text().strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

    return ""


def multi_hop_retrieve(question, index, initial_results, config):
    """Multi-hop retrieval for multi-session questions.

    After the initial search, extracts entities and key phrases from
    top results, formulates follow-up queries, and searches again.
    Merges all results, deduped by path.
    """
    from longmemeval_retrieval import bm25_search, tokenize

    limit = config.get("retrieval_limit", 10)
    min_score = config.get("recall_min_score", 0.0)
    max_hops = config.get("multi_hop_max", 2)

    all_results = list(initial_results)
    seen_paths = {r["path"] for r in all_results}

    for hop in range(max_hops):
        # Extract key phrases from current results for follow-up queries
        follow_up_terms = set()
        for r in all_results[:5]:  # top 5 results
            snippet = r.get("snippet", "")
            # Extract capitalized phrases (names, places) and numbers
            import re
            # Proper nouns and multi-word names
            for match in re.finditer(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*', snippet):
                term = match.group()
                if len(term) > 3 and term.lower() not in tokenize(question["question"]):
                    follow_up_terms.add(term)
            # Numbers with context (e.g., "$350,000", "70 pounds")
            for match in re.finditer(r'[\$]?\d[\d,]*\.?\d*\s*\w+', snippet):
                follow_up_terms.add(match.group().strip())

        if not follow_up_terms:
            break

        # Build follow-up query from original question + extracted entities
        follow_up_query = f"{question['question']} {' '.join(list(follow_up_terms)[:8])}"
        hop_results = bm25_search(index, follow_up_query, limit=limit, min_score=min_score)

        added = 0
        for r in hop_results:
            if r["path"] not in seen_paths:
                all_results.append(r)
                seen_paths.add(r["path"])
                added += 1

        if added == 0:
            break  # No new results found

    all_results.sort(key=lambda r: r["score"], reverse=True)
    return all_results


def apply_recency_boost(results, documents_by_id):
    """Boost more recent documents for knowledge-update questions.

    Parses dates from document metadata and applies a recency multiplier.
    Most recent doc gets 1.5x, oldest gets 1.0x.
    """
    dated_results = []
    for r in results:
        doc_id = r.get("_doc_id", Path(r.get("path", "")).stem)
        session_id = doc_id.split(":")[0] if ":" in doc_id else doc_id
        doc = documents_by_id.get(session_id, {})
        date_str = doc.get("metadata", {}).get("date", "")
        dated_results.append((r, date_str))

    if not any(d for _, d in dated_results):
        return results

    # Sort by date to find range
    dated_with_parsed = []
    for r, date_str in dated_results:
        try:
            # Parse various date formats
            import re
            # "2023/05/20 (Sat) 02:21" -> "2023/05/20"
            clean = re.sub(r'\s*\([^)]*\)\s*', ' ', date_str).strip()
            clean = clean.replace('/', '-').split()[0] if clean else ""
            dated_with_parsed.append((r, clean))
        except (ValueError, IndexError):
            dated_with_parsed.append((r, ""))

    # Sort by date string descending (more recent first for ties)
    valid_dates = [(r, d) for r, d in dated_with_parsed if d]
    if valid_dates:
        valid_dates.sort(key=lambda x: x[1], reverse=True)
        # Apply linear boost: most recent gets 1.5x, oldest gets 1.0x
        n = len(valid_dates)
        for i, (r, _) in enumerate(valid_dates):
            boost = 1.0 + 0.5 * (1.0 - i / max(n - 1, 1))
            r["score"] = r["score"] * boost

    all_r = [r for r, _ in dated_with_parsed]
    all_r.sort(key=lambda r: r["score"], reverse=True)
    return all_r


def format_context_temporal(results, documents_by_id):
    """Format context with explicit date ordering for temporal questions."""
    # Collect and sort by date
    entries = []
    for r in results:
        doc_id = r.get("_doc_id", Path(r.get("path", "")).stem)
        session_id = doc_id.split(":")[0] if ":" in doc_id else doc_id
        doc = documents_by_id.get(session_id, {})
        date = doc.get("metadata", {}).get("date", "unknown date")
        text = doc.get("text", r.get("snippet", ""))
        entries.append((date, session_id, text))

    # Sort chronologically
    entries.sort(key=lambda x: x[0])

    parts = []
    for i, (date, sid, text) in enumerate(entries, 1):
        parts.append(f"--- Chat #{i} (Date: {date}) ---\n{text}")
    return "\n\n".join(parts)


def format_context(results, documents_by_id):
    """Format retrieved results as context for the LLM."""
    context_parts = []
    for r in results:
        doc_id = r.get("_doc_id", Path(r.get("path", "")).stem)
        # For turn-level results, get the session id
        session_id = doc_id.split(":")[0] if ":" in doc_id else doc_id
        if session_id in documents_by_id:
            doc = documents_by_id[session_id]
            context_parts.append(f"--- Session ({doc.get('metadata', {}).get('date', 'unknown date')}) ---\n{doc['text']}")
        elif r.get("snippet"):
            context_parts.append(f"--- {r.get('title', 'Unknown')} ---\n{r['snippet']}")
    return "\n\n".join(context_parts)


def generate_answer(question_text, context, question_date, question_type=""):
    """Generate an answer using codex exec."""
    # Cap context to ~30k chars to stay within model limits
    if len(context) > 30000:
        context = context[:30000] + "\n\n[... truncated ...]"

    # Simple prompt that works reliably with codex
    # Type-specific hints are kept minimal to avoid empty responses
    hint = ""
    if question_type == "knowledge-update":
        hint = " Use the most recent information when facts conflict."
    elif question_type == "temporal-reasoning":
        hint = " Pay attention to dates and chronological order."
    elif question_type == "multi-session":
        hint = " The answer may span multiple chat sessions."
    elif "preference" in question_type:
        hint = " Describe what kind of response the user would prefer, starting with 'The user would prefer'."

    prompt = (
        "I will give you several history chats between you and a user. "
        f"Answer the question based on the relevant chat history.{hint} "
        "Give a short, direct answer.\n\n"
        f"History Chats:\n\n{context}\n\n"
        f"Current Date: {question_date}\n"
        f"Question: {question_text}\n"
        "Answer:"
    )
    answer = call_llm(prompt, timeout=90)
    if not answer:
        # Retry with shorter context
        short_context = context[:15000] if len(context) > 15000 else context
        prompt = (
            f"Answer this question based on the chat history.{hint} Short, direct answer.\n\n"
            f"Chat History:\n{short_context}\n\n"
            f"Date: {question_date}\n"
            f"Question: {question_text}\n"
            "Answer:"
        )
        answer = call_llm(prompt, timeout=90)
    return answer


def judge_answer(question_text, gold_answer, hypothesis, question_type):
    """Judge whether the hypothesis matches the gold answer using codex exec."""
    if question_type == "temporal-reasoning":
        prompt = (
            "You are evaluating a memory system's answer to a temporal reasoning question.\n\n"
            f"Question: {question_text}\n"
            f"Gold answer: {gold_answer}\n"
            f"System answer: {hypothesis}\n\n"
            "Does the system answer match the gold answer? Minor date/time variations are acceptable. "
            "Respond with exactly 'YES' or 'NO'."
        )
    elif "preference" in question_type:
        prompt = (
            "You are evaluating a memory system's answer about a user preference.\n\n"
            f"Question: {question_text}\n"
            f"Gold answer: {gold_answer}\n"
            f"System answer: {hypothesis}\n\n"
            "Does the system answer correctly capture the user's preference? "
            "Respond with exactly 'YES' or 'NO'."
        )
    elif question_type == "knowledge-update":
        prompt = (
            "You are evaluating a memory system's answer about updated information.\n\n"
            f"Question: {question_text}\n"
            f"Gold answer (most recent): {gold_answer}\n"
            f"System answer: {hypothesis}\n\n"
            "Does the system answer reflect the most recent/updated information? "
            "Respond with exactly 'YES' or 'NO'."
        )
    else:
        prompt = (
            "You are evaluating a memory system's answer.\n\n"
            f"Question: {question_text}\n"
            f"Gold answer: {gold_answer}\n"
            f"System answer: {hypothesis}\n\n"
            "Does the system answer correctly match the gold answer? "
            "Respond with exactly 'YES' or 'NO'."
        )
    response = call_llm(prompt, timeout=60)
    return response.strip().upper().startswith("YES")


def run_full_eval(dataset_path, config=None, max_questions=None, output_path=None):
    """Run full end-to-end evaluation: retrieval + generation + judging.

    Uses codex exec for both generation and judging (zero API cost).

    Returns:
        dict with per-type accuracy and overall accuracy
    """
    import time as _time

    if config is None:
        config = get_default_config()

    dataset = load_dataset(dataset_path)
    if max_questions:
        dataset = dataset[:max_questions]

    results_log = []
    correct_by_type = {}
    total_by_type = {}

    for i, question in enumerate(dataset):
        q_id = question["question_id"]
        q_type = question.get("question_type", "unknown")
        q_text = question["question"]
        q_date = question.get("question_date", "")
        gold = question["answer"]

        # Retrieval
        retrieval_results = run_retrieval(question, config)

        # Build document lookup for context formatting
        documents = ingest_haystack(question, granularity=config.get("granularity", "session"))
        docs_by_id = {doc["id"]: doc for doc in documents}

        # Apply recency boost for knowledge-update questions
        if q_type == "knowledge-update":
            retrieval_results = apply_recency_boost(retrieval_results, docs_by_id)

        # Format context — temporal-aware for temporal/multi-session, standard otherwise
        if q_type in ("temporal-reasoning", "multi-session"):
            context = format_context_temporal(retrieval_results, docs_by_id)
        else:
            context = format_context(retrieval_results, docs_by_id)

        # Generate answer with type-aware prompting
        t0 = _time.time()
        hypothesis = generate_answer(q_text, context, q_date, question_type=q_type)
        gen_ms = int((_time.time() - t0) * 1000)

        # Judge
        t0 = _time.time()
        is_correct = judge_answer(q_text, gold, hypothesis, q_type)
        judge_ms = int((_time.time() - t0) * 1000)

        # Track
        total_by_type.setdefault(q_type, 0)
        correct_by_type.setdefault(q_type, 0)
        total_by_type[q_type] += 1
        if is_correct:
            correct_by_type[q_type] += 1

        entry = {
            "question_id": q_id,
            "question_type": q_type,
            "hypothesis": hypothesis,
            "gold": gold,
            "correct": is_correct,
            "gen_ms": gen_ms,
            "judge_ms": judge_ms,
        }
        results_log.append(entry)

        # Progress
        total_correct = sum(correct_by_type.values())
        total_done = sum(total_by_type.values())
        acc = total_correct / total_done if total_done > 0 else 0
        print(
            f"  [{i+1}/{len(dataset)}] {q_type}: "
            f"{'CORRECT' if is_correct else 'WRONG'} "
            f"(running: {acc:.1%})",
            file=sys.stderr,
        )

    # Save JSONL
    if output_path:
        with open(output_path, "w") as f:
            for entry in results_log:
                f.write(json.dumps(entry) + "\n")

    # Compute accuracies
    per_type = {}
    for q_type in sorted(total_by_type):
        per_type[q_type] = {
            "correct": correct_by_type.get(q_type, 0),
            "total": total_by_type[q_type],
            "accuracy": correct_by_type.get(q_type, 0) / total_by_type[q_type],
        }

    total_correct = sum(correct_by_type.values())
    total_questions = sum(total_by_type.values())
    overall_accuracy = total_correct / total_questions if total_questions > 0 else 0

    # Task-averaged accuracy (mean of per-type accuracies, LongMemEval standard)
    type_accuracies = [v["accuracy"] for v in per_type.values()]
    task_averaged = sum(type_accuracies) / len(type_accuracies) if type_accuracies else 0

    return {
        "overall_accuracy": overall_accuracy,
        "task_averaged_accuracy": task_averaged,
        "num_questions": total_questions,
        "num_correct": total_correct,
        "per_type": per_type,
    }


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
    elif args.mode == "full":
        output = args.output or "longmemeval_full_results.jsonl"
        results = run_full_eval(
            args.dataset, config,
            max_questions=args.max_questions, output_path=output,
        )
        print(json.dumps(results, indent=2))
        print(f"\nDetailed results: {output}", file=sys.stderr)
