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

from memento_utils import get_config, get_vault, has_qmd, qmd_search_with_extras, enhance_results, detect_project, log_retrieval, read_hook_input, is_vsearch_warm, rrf_fuse, mark_vsearch_warm

LAST_RECALL_PATH = "/tmp/memento-last-recall.json"
DEFERRED_BRIEFING_PATH = "/tmp/memento-deferred-briefing.json"


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


def format_result(result):
    """Format a QMD result as a compact one-liner."""
    title = result.get("title", "")
    snippet = result.get("snippet", "").strip()

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

    if config.get("rrf_enabled", True) and is_vsearch_warm():
        # Warm path: BM25 + vsearch in parallel, fuse with RRF
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            bm25_future = pool.submit(
                qmd_search_with_extras, query,
                limit=max_notes + 4, semantic=False, timeout=5, min_score=min_score,
            )
            vec_future = pool.submit(
                qmd_search_with_extras, query,
                limit=max_notes + 4, semantic=True, timeout=5, min_score=min_score,
            )
            bm25_results = bm25_future.result()
            vec_results = vec_future.result()

        results = rrf_fuse([bm25_results, vec_results], k=config.get("rrf_k", 60))
    else:
        # Cold path: BM25 only (current behavior)
        results = qmd_search_with_extras(
            query, limit=max_notes + 4, semantic=False, timeout=5, min_score=min_score,
        )

    latency_ms = int((time.time() - t0) * 1000)
    results_before = len(results)

    # Supplement with concept index if enabled
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
            pass  # Non-fatal — concept index is supplementary

    if not results:
        bump_prompts_since()
        log_retrieval("recall", "no-results", query=query, latency_ms=latency_ms)
        return [], None

    results = enhance_results(results, config, cwd=cwd)

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
                  injected_titles=injected, injected_chars=len(injected_text))

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
