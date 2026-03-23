#!/usr/bin/env python3
"""
Vault recall — UserPromptSubmit hook.
Runs JIT semantic search against the user's prompt and prints
relevant vault notes to stdout so Claude sees them before processing.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

# Allow imports from the same directory
sys.path.insert(0, str(Path(__file__).parent))

from memento_utils import get_config, get_vault, has_qmd, qmd_search, qmd_search_with_extras, enhance_results, detect_project, log_retrieval, read_hook_input, is_vsearch_warm, rrf_fuse, mark_vsearch_warm, prf_expand_query, needs_multi_hop, multi_hop_search, RUNTIME_DIR

LAST_RECALL_PATH = os.path.join(RUNTIME_DIR, "last-recall.json")
DEFERRED_BRIEFING_PATH = os.path.join(RUNTIME_DIR, "deferred-briefing.json")


def should_skip(prompt, config):
    """Relevance gate — returns True if we should skip vault injection."""
    prompt = prompt.strip()

    # Too short
    if len(prompt) < 10:
        return True

    # Skill invocation
    if prompt.startswith("/"):
        return True

    # Skill expansions and command messages (XML tags from hook system)
    if "<command-message>" in prompt or "<command-name>" in prompt:
        return True
    if "<task-notification>" in prompt:
        return True

    # Skill content dumps (headers from expanded skills)
    if prompt.startswith("# ") and len(prompt) > 300:
        return True

    # Very long prompts are almost always skill expansions, not user input
    if len(prompt) > 500:
        return True

    # Match skip patterns
    skip_patterns = config.get("recall_skip_patterns", [])
    prompt_lower = prompt.lower().strip()
    for pattern in skip_patterns:
        try:
            if re.match(pattern, prompt_lower, re.IGNORECASE):
                return True
        except re.error:
            continue

    return False


def is_duplicate(top_result_path):
    """Check if the top result is the same as the last injection.
    Returns True if we should skip to avoid repetition.
    """
    try:
        if not os.path.exists(LAST_RECALL_PATH):
            return False

        with open(LAST_RECALL_PATH) as f:
            last = json.load(f)

        # Skip if same top result within the last 3 prompts
        if last.get("top_path") == top_result_path:
            prompts_since = last.get("prompts_since", 0)
            if prompts_since < 3:
                # Update counter
                last["prompts_since"] = prompts_since + 1
                with open(LAST_RECALL_PATH, "w") as f:
                    json.dump(last, f)
                return True

        return False

    except Exception:
        return False


def record_recall(top_result_path):
    """Record this recall for dedup tracking."""
    try:
        with open(LAST_RECALL_PATH, "w") as f:
            json.dump({
                "top_path": top_result_path,
                "prompts_since": 0,
                "timestamp": time.time(),
            }, f)
    except Exception:
        pass


def bump_prompts_since():
    """Increment the prompts_since counter when we skip injection."""
    try:
        if not os.path.exists(LAST_RECALL_PATH):
            return

        with open(LAST_RECALL_PATH) as f:
            last = json.load(f)

        last["prompts_since"] = last.get("prompts_since", 0) + 1
        with open(LAST_RECALL_PATH, "w") as f:
            json.dump(last, f)

    except Exception:
        pass


def _strip_injection(text):
    """Strip instruction-like patterns from injected content (defense-in-depth)."""
    if not text:
        return text
    # Remove patterns that could be interpreted as system instructions
    text = re.sub(r"(?i)(ignore\s+(all\s+)?previous\s+instructions)", "[filtered]", text)
    text = re.sub(r"(?i)(you\s+are\s+now\s+|you\s+must\s+now\s+)", "[filtered]", text)
    text = re.sub(r"(?i)^(system|assistant)\s*:", "[filtered]:", text)
    text = re.sub(r"</?s>", "", text)
    return text


def format_result(result):
    """Format a QMD result as a compact one-liner."""
    title = _strip_injection(result.get("title", ""))
    snippet = _strip_injection(result.get("snippet", "").strip())

    # Truncate snippet to first sentence or 120 chars
    if snippet:
        dot = snippet.find(".")
        if 0 < dot < 120:
            snippet = snippet[:dot + 1]
        elif len(snippet) > 120:
            snippet = snippet[:120] + "..."

    line = f"  - {title}"
    if snippet:
        line += f": {snippet}"
    return line


def consume_deferred_briefing():
    """Check for deferred briefing from SessionStart and consume it.

    Returns formatted lines to prepend, or empty list.
    If the background search is still pending, leaves the file intact
    so the next prompt can pick it up. Only deletes on successful
    consumption or if the file is stale (>60s).
    """
    try:
        if not os.path.exists(DEFERRED_BRIEFING_PATH):
            return []

        with open(DEFERRED_BRIEFING_PATH) as f:
            data = json.load(f)

        status = data.get("status", "")

        if status == "pending":
            # Background worker still running — check staleness
            ts = data.get("params", {}).get("timestamp", 0)
            if ts and (time.time() - ts) > 60:
                # Stale pending file — worker probably crashed
                os.unlink(DEFERRED_BRIEFING_PATH)
            # Either way, nothing to inject yet
            return []

        if status != "ready":
            os.unlink(DEFERRED_BRIEFING_PATH)
            return []

        # Got results — consume and clean up
        note_lines = data.get("note_lines", [])
        os.unlink(DEFERRED_BRIEFING_PATH)

        # Mark vsearch as warm for RRF hybrid search
        try:
            mark_vsearch_warm()
        except Exception:
            pass

        if not note_lines:
            return []

        return ["[vault] Relevant notes:"] + note_lines

    except (json.JSONDecodeError, OSError, KeyError):
        try:
            os.unlink(DEFERRED_BRIEFING_PATH)
        except OSError:
            pass
        return []


def run_recall():
    """Run the recall search. Returns (lines, top_path) or ([], None)."""
    config = get_config()

    if not config.get("prompt_recall", True):
        return [], None

    vault = get_vault()
    if not vault.exists() or not (vault / "notes").exists():
        return [], None

    if not has_qmd():
        return [], None

    try:
        hook_input = read_hook_input()
    except Exception:
        return [], None

    prompt = hook_input.get("prompt", "")
    cwd = hook_input.get("cwd", "")
    if not prompt:
        return [], None

    if should_skip(prompt, config):
        bump_prompts_since()
        return [], None

    # BM25 search against the prompt, augmented with project context
    min_score = config.get("recall_min_score", 0.4)
    max_notes = config.get("recall_max_notes", 3)

    # Bias toward current project by appending project slug to query
    query = prompt
    if cwd:
        project_slug, _ = detect_project(cwd, None)
        if project_slug and project_slug != "unknown":
            query = f"{prompt} {project_slug.replace('-', ' ')}"

    t0 = time.time()

    # Adaptive pipeline: BM25 first, decide depth based on confidence.
    #
    # Fast path (BM25 score >= threshold): BM25 + enhance_results only.
    #   This is the v1.1.0 path. ~800ms.
    #
    # Deep path (BM25 score < threshold): PRF expand + RRF fuse + CE rerank.
    #   PRF reuses initial results (no extra QMD call for term extraction).
    #   PRF expanded query runs one additional BM25 call.
    #   RRF adds one vsearch call (only when warm).
    #   CE reranks the fused results.
    #
    # The threshold is intentionally low (0.55) because BM25 scores for
    # natural language prompts against vault notes typically range 0.4-0.8.
    # At 0.55+, BM25 has found a reasonable match and the extra stages
    # add latency without much quality gain.

    high_conf = config.get("recall_high_confidence", 0.55)
    search_limit = max_notes + 4

    results = qmd_search_with_extras(
        query, limit=search_limit, semantic=False, timeout=5, min_score=min_score,
    )
    top_score = results[0]["score"] if results else 0
    pipeline_depth = "bm25"

    if top_score < high_conf and results:
        # Low confidence — try harder with PRF + RRF

        # PRF: expand query using terms from the results we already have (zero extra QMD calls)
        expanded_query = prf_expand_query(query, config=config, initial_results=results)
        if expanded_query != query:
            prf_results = qmd_search_with_extras(
                expanded_query, limit=search_limit,
                semantic=False, timeout=5, min_score=min_score,
            )
            if prf_results:
                existing = {r["path"] for r in results}
                for r in prf_results:
                    if r["path"] not in existing:
                        results.append(r)
                        existing.add(r["path"])
                results.sort(key=lambda r: r["score"], reverse=True)
                pipeline_depth = "prf"

        # RRF: fuse with vsearch when warm
        if config.get("rrf_enabled", True) and is_vsearch_warm():
            vec_results = qmd_search_with_extras(
                query, limit=search_limit,
                semantic=True, timeout=5, min_score=min_score,
            )
            if vec_results:
                results = rrf_fuse([results, vec_results], k=config.get("rrf_k", 60))
                pipeline_depth = "rrf"

    latency_ms = int((time.time() - t0) * 1000)
    results_before = len(results)

    # Concept index supplement (always, O(1) lookup)
    if config.get("concept_index_enabled", True):
        try:
            from memento_utils import lookup_concepts
            concept_hits = lookup_concepts(prompt)
            if concept_hits:
                existing_paths = {r.get("path", "") for r in results}
                for hit in concept_hits:
                    if hit["path"] not in existing_paths:
                        hit["score"] = max(hit.get("score", 0), config.get("concept_index_score", 0.5))
                        results.append(hit)
                        existing_paths.add(hit["path"])
        except Exception:
            pass

    # Multi-hop retrieval (experimental, deep path only)
    multi_hop_gate = (top_score < high_conf
                      and config.get("multi_hop_enabled", False)
                      and needs_multi_hop(prompt))
    multi_hop_added = 0
    if multi_hop_gate and results:
        try:
            pre_hop_count = len(results)
            results = multi_hop_search(prompt, results, config=config)
            multi_hop_added = len(results) - pre_hop_count
            pipeline_depth += "+hop"
        except Exception:
            pass

    if not results:
        bump_prompts_since()
        log_retrieval("recall", "no-results", query=query, latency_ms=latency_ms,
                      pipeline=pipeline_depth)
        return [], None

    results = enhance_results(results, config, cwd=cwd)

    # CE reranking (only on deep path)
    if top_score < high_conf and config.get("reranker_enabled", True) and len(results) > 1:
        try:
            from tenet_reranker import rerank
            results = rerank(prompt, results, config)
            pipeline_depth += "+ce"
        except Exception:
            pass

    if not results:
        bump_prompts_since()
        log_retrieval("recall", "filtered-empty", query=query,
                      results_before=results_before, latency_ms=latency_ms)
        return [], None

    top_path = results[0].get("path", "")
    if is_duplicate(top_path):
        log_retrieval("recall", "dedup-skip", query=query)
        return [], None

    lines = ["[vault] Related memories:"]
    injected = []
    for result in results[:max_notes]:
        lines.append(format_result(result))
        injected.append(result.get("title", ""))

    injected_text = "\n".join(lines)
    log_retrieval("recall", "inject", query=query, latency_ms=latency_ms,
                  results_before=results_before, results_after=len(results),
                  injected_titles=injected, injected_chars=len(injected_text),
                  pipeline=pipeline_depth,
                  multi_hop_gate=multi_hop_gate, multi_hop_added=multi_hop_added)

    return lines, top_path


def main():
    # Always check for deferred briefing, even if recall is disabled
    deferred_lines = consume_deferred_briefing()
    recall_lines, top_path = run_recall()

    output = deferred_lines + recall_lines
    if output:
        print("\n".join(output))

    if top_path:
        record_recall(top_path)


if __name__ == "__main__":
    main()
