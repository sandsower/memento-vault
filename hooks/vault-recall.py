#!/usr/bin/env python3
"""
Vault recall — UserPromptSubmit hook.
Runs JIT semantic search against the user's prompt and prints
relevant vault notes to stdout so Claude sees them before processing.
"""

import json
import os
import re
import subprocess as _subprocess
import sys
import tempfile
import time
from pathlib import Path

# Allow imports from the repo and same directory
_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(Path(__file__).parent))

from memento.config import RUNTIME_DIR, detect_project, get_config, get_vault  # noqa: E402
from memento.graph import lookup_concepts  # noqa: E402
from memento.search import (  # noqa: E402
    enhance_results,
    has_qmd,
    is_vsearch_warm,
    mark_vsearch_warm,
    multi_hop_search,
    prf_expand_query,
    qmd_search_with_extras,
    rrf_fuse,
)
from memento.llm import llm_complete  # noqa: E402
from memento.store import log_retrieval  # noqa: E402
from memento.utils import read_hook_input  # noqa: E402

LAST_RECALL_PATH = os.path.join(RUNTIME_DIR, "last-recall.json")
DEFERRED_BRIEFING_PATH = os.path.join(RUNTIME_DIR, "deferred-briefing.json")
DEEP_RECALL_PENDING_PATH = os.path.join(RUNTIME_DIR, "deep-recall-pending.json")


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
    if prompt.startswith("# ") and len(prompt) > 200:
        return True

    # Ticket context injections from start-ticket and similar skills
    if "You are working on" in prompt:
        return True
    if prompt.startswith("Continuation guidance:"):
        return True

    # Local command caveats
    if "<local-command-caveat>" in prompt:
        return True

    # Long prompts are almost always skill expansions, not user input
    # Real user prompts rarely exceed 200 chars
    if len(prompt) > 200:
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
            json.dump(
                {
                    "top_path": top_result_path,
                    "prompts_since": 0,
                    "timestamp": time.time(),
                },
                f,
            )
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
            snippet = snippet[: dot + 1]
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


def spawn_deep_recall(prompt, initial_results, config):
    """Spawn a background codex process for deeper vault analysis.

    Writes the prompt + initial results to a temp input file, then spawns
    a detached process that calls codex and writes structured output to
    DEEP_RECALL_PENDING_PATH.

    Only called when the prompt is complex (multi-hop gate + low confidence).
    """
    backend = config.get("deep_recall_backend", "codex")

    # Build context from initial results
    context_lines = []
    for r in initial_results[:5]:
        title = r.get("title", "")
        snippet = r.get("snippet", "").strip()
        path = r.get("path", "")
        context_lines.append(f"- {title} ({path}): {snippet[:200]}")

    input_data = {
        "prompt": prompt,
        "initial_results": context_lines,
        "timestamp": time.time(),
    }

    try:
        # Write input for the background worker
        input_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="deep-recall-input-",
            dir=RUNTIME_DIR,
            delete=False,
        )
        json.dump(input_data, input_file)
        input_file.close()

        # Mark as pending
        with open(DEEP_RECALL_PENDING_PATH, "w") as f:
            json.dump({"status": "pending", "timestamp": time.time()}, f)

        # Spawn background worker
        _subprocess.Popen(
            [sys.executable, __file__, "--deep-recall", input_file.name, backend],
            stdin=_subprocess.DEVNULL,
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        # Clean up on spawn failure
        try:
            os.unlink(DEEP_RECALL_PENDING_PATH)
        except OSError:
            pass
        try:
            os.unlink(input_file.name)
        except (OSError, UnboundLocalError):
            pass


def run_deep_recall_worker(input_path, backend):
    """Background worker: run codex analysis and write results.

    Called as a detached subprocess via --deep-recall flag.
    """
    try:
        with open(input_path) as f:
            input_data = json.load(f)
    except (OSError, json.JSONDecodeError):
        _cleanup_deep_recall_pending()
        return
    finally:
        try:
            os.unlink(input_path)
        except OSError:
            pass

    prompt = input_data.get("prompt", "")
    context_lines = input_data.get("initial_results", [])

    if not prompt:
        _cleanup_deep_recall_pending()
        return

    context_block = "\n".join(context_lines) if context_lines else "(no initial results)"

    codex_prompt = (
        "You are a vault recall assistant. The user asked:\n\n"
        f"{prompt}\n\n"
        "Initial search found these vault notes:\n"
        f"{context_block}\n\n"
        "Based on the user's question and the notes above, identify what "
        "additional vault notes would be relevant. Think about:\n"
        "- Related decisions or patterns from other projects\n"
        "- Temporal context (what changed, previous approaches)\n"
        "- Cross-references between the found notes\n\n"
        "Return a JSON array of objects with 'title' and 'reason' keys. "
        "Each title should be the likely title of a vault note that would help. "
        "Return at most 3 suggestions. If nothing additional is needed, return []."
    )

    try:
        result = llm_complete(
            codex_prompt,
            {
                "llm_backend": backend,
            },
        )
        raw = result.text if result.ok else ""

        # Parse the LLM response — extract JSON array
        suggestions = _parse_deep_recall_response(raw)

        with open(DEEP_RECALL_PENDING_PATH, "w") as f:
            json.dump(
                {
                    "status": "ready",
                    "suggestions": suggestions,
                    "prompt": prompt,
                    "timestamp": time.time(),
                },
                f,
            )

    except OSError:
        _cleanup_deep_recall_pending()


def _parse_deep_recall_response(raw):
    """Extract a JSON array of suggestions from the LLM response."""
    if not raw:
        return []

    # Try direct JSON parse first
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [s for s in parsed if isinstance(s, dict) and "title" in s][:3]
    except json.JSONDecodeError:
        pass

    # Try extracting JSON from markdown code blocks
    match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, list):
                return [s for s in parsed if isinstance(s, dict) and "title" in s][:3]
        except json.JSONDecodeError:
            pass

    # Try finding a bare JSON array in the text
    match = re.search(r"\[.*?\]", raw, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                return [s for s in parsed if isinstance(s, dict) and "title" in s][:3]
        except json.JSONDecodeError:
            pass

    return []


def _cleanup_deep_recall_pending():
    """Remove the pending file on worker failure."""
    try:
        os.unlink(DEEP_RECALL_PENDING_PATH)
    except OSError:
        pass


def consume_deep_recall():
    """Check for pending deep recall results and consume them.

    Returns formatted lines to inject, or empty list.
    Same pattern as consume_deferred_briefing: pending = wait,
    ready = consume, stale (>60s) = discard.
    """
    try:
        if not os.path.exists(DEEP_RECALL_PENDING_PATH):
            return []

        with open(DEEP_RECALL_PENDING_PATH) as f:
            data = json.load(f)

        status = data.get("status", "")

        if status == "pending":
            ts = data.get("timestamp", 0)
            if ts and (time.time() - ts) > 60:
                os.unlink(DEEP_RECALL_PENDING_PATH)
            return []

        if status != "ready":
            os.unlink(DEEP_RECALL_PENDING_PATH)
            return []

        suggestions = data.get("suggestions", [])
        os.unlink(DEEP_RECALL_PENDING_PATH)

        if not suggestions:
            return []

        lines = ["[vault] Deep analysis suggests also reviewing:"]
        for s in suggestions[:3]:
            title = _strip_injection(s.get("title", ""))
            reason = _strip_injection(s.get("reason", ""))
            if title:
                line = f"  - {title}"
                if reason:
                    line += f": {reason}"
                lines.append(line)

        return lines if len(lines) > 1 else []

    except (json.JSONDecodeError, OSError, KeyError):
        try:
            os.unlink(DEEP_RECALL_PENDING_PATH)
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
        query,
        limit=search_limit,
        semantic=False,
        timeout=5,
        min_score=min_score,
    )
    top_score = results[0]["score"] if results else 0
    pipeline_depth = "bm25"

    if top_score < high_conf and results:
        # Low confidence — try harder with PRF + RRF

        # PRF: expand query using terms from the results we already have (zero extra QMD calls)
        expanded_query = prf_expand_query(query, config=config, initial_results=results)
        if expanded_query != query:
            prf_results = qmd_search_with_extras(
                expanded_query,
                limit=search_limit,
                semantic=False,
                timeout=5,
                min_score=min_score,
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
                query,
                limit=search_limit,
                semantic=True,
                timeout=5,
                min_score=min_score,
            )
            if vec_results:
                results = rrf_fuse([results, vec_results], k=config.get("rrf_k", 60))
                pipeline_depth = "rrf"

    latency_ms = int((time.time() - t0) * 1000)
    results_before = len(results)

    # Concept index supplement (always, O(1) lookup)
    if config.get("concept_index_enabled", True):
        try:
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

    # Multi-hop retrieval: follow wikilinks from top results
    multi_hop_gate = top_score < high_conf and config.get("multi_hop_enabled", False)
    multi_hop_added = 0
    if multi_hop_gate and results:
        try:
            pre_hop_count = len(results)
            results = multi_hop_search(prompt, results, config=config)
            multi_hop_added = len(results) - pre_hop_count
            pipeline_depth += "+hop"
        except Exception:
            pass

    # Deep recall: spawn background codex for complex prompts
    # Gate: low confidence AND feature enabled
    deep_recall_spawned = False
    if (
        top_score < high_conf
        and config.get("deep_recall_enabled", False)
        and results
        and not os.path.exists(DEEP_RECALL_PENDING_PATH)
    ):
        try:
            spawn_deep_recall(prompt, results, config)
            deep_recall_spawned = True
            pipeline_depth += "+deep"
        except Exception:
            pass

    if not results:
        bump_prompts_since()
        log_retrieval("recall", "no-results", query=query, latency_ms=latency_ms, pipeline=pipeline_depth)
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
        log_retrieval("recall", "filtered-empty", query=query, results_before=results_before, latency_ms=latency_ms)
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
    log_retrieval(
        "recall",
        "inject",
        query=query,
        latency_ms=latency_ms,
        results_before=results_before,
        results_after=len(results),
        injected_titles=injected,
        injected_chars=len(injected_text),
        pipeline=pipeline_depth,
        multi_hop_gate=multi_hop_gate,
        multi_hop_added=multi_hop_added,
        deep_recall_spawned=deep_recall_spawned,
    )

    return lines, top_path


def main():
    # Handle background worker mode
    if len(sys.argv) >= 4 and sys.argv[1] == "--deep-recall":
        run_deep_recall_worker(sys.argv[2], sys.argv[3])
        return

    # Always check for deferred briefing, even if recall is disabled
    deferred_lines = consume_deferred_briefing()

    # Check for deep recall results from a previous prompt's background run
    deep_recall_lines = consume_deep_recall()

    recall_lines, top_path = run_recall()

    output = deferred_lines + deep_recall_lines + recall_lines
    if output:
        print("\n".join(output))

    if top_path:
        record_recall(top_path)


if __name__ == "__main__":
    main()
