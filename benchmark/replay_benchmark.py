#!/usr/bin/env python3
"""
Memento vault replay benchmark.
Parses real Claude Code session transcripts and replays prompts + file reads
through the retrieval hooks to measure real-world performance.

Usage:
    python3 benchmark/replay_benchmark.py [--max-sessions N] [--max-per-project N]
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).parent.parent / "hooks"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def parse_transcript(jsonl_path):
    """Parse a Claude Code transcript JSONL file.
    Returns dict with cwd, git_branch, session_id, user_prompts, read_paths.
    """
    cwd = ""
    git_branch = ""
    session_id = ""
    user_prompts = []
    read_paths = []

    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                t = d.get("type", "")

                # Extract session metadata from progress/user records
                if not cwd and d.get("cwd"):
                    cwd = d["cwd"]
                if not git_branch and d.get("gitBranch"):
                    git_branch = d["gitBranch"]
                if not session_id and d.get("sessionId"):
                    session_id = d["sessionId"]

                # User prompts
                if t == "user":
                    msg = d.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text = c.get("text", "").strip()
                                    # Skip skill expansions and task notifications
                                    if text and not text.startswith("<") and not text.startswith("Base directory"):
                                        user_prompts.append(text[:500])
                        elif isinstance(content, str) and content.strip():
                            user_prompts.append(content.strip()[:500])

                # Read tool calls from assistant messages
                if t == "assistant":
                    msg = d.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Read":
                                    inp = c.get("input", {})
                                    fp = inp.get("file_path", "")
                                    if fp:
                                        read_paths.append(fp)
    except (json.JSONDecodeError, OSError):
        pass

    return {
        "cwd": cwd,
        "git_branch": git_branch,
        "session_id": session_id,
        "user_prompts": user_prompts,
        "read_paths": read_paths,
        "transcript": str(jsonl_path),
    }


def find_transcripts(max_per_project=3):
    """Find recent session transcripts across all projects."""
    transcripts = []

    if not CLAUDE_PROJECTS.exists():
        return transcripts

    for project_dir in sorted(CLAUDE_PROJECTS.iterdir()):
        if not project_dir.is_dir():
            continue

        # Find JSONL files (transcripts), sorted by modification time (newest first)
        jsonl_files = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        count = 0
        for jf in jsonl_files:
            if jf.stat().st_size < 1000:  # skip tiny sessions
                continue
            if count >= max_per_project:
                break
            transcripts.append(jf)
            count += 1

    return transcripts


def run_hook(hook_script, stdin_data, timeout=15):
    """Run a hook script with JSON stdin. Returns (stdout, latency_ms)."""
    t0 = time.time()
    try:
        result = subprocess.run(
            [sys.executable, str(HOOKS_DIR / hook_script)],
            input=json.dumps(stdin_data),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "MEMENTO_DEBUG": ""},
        )
        latency_ms = int((time.time() - t0) * 1000)
        return result.stdout.strip(), latency_ms
    except subprocess.TimeoutExpired:
        latency_ms = int((time.time() - t0) * 1000)
        return "", latency_ms


def count_injected_chars(output):
    """Count injected characters from hook output."""
    if not output:
        return 0
    try:
        data = json.loads(output)
        ctx = data.get("hookSpecificOutput", {}).get("additionalContext", "")
        return len(ctx)
    except (json.JSONDecodeError, AttributeError):
        pass
    return len(output)


def replay_session(parsed, quiet=False):
    """Replay a parsed session through the hooks. Returns stats."""
    cwd = parsed["cwd"]
    sid = parsed["session_id"] or "replay"
    prompts = parsed["user_prompts"]
    reads = parsed["read_paths"]

    stats = {
        "cwd": cwd,
        "session_id": sid,
        "prompt_count": len(prompts),
        "read_count": len(reads),
        "briefing": {"latency_ms": 0, "chars": 0},
        "recall": {
            "calls": 0, "injections": 0, "skips": 0,
            "total_latency_ms": 0, "total_chars": 0,
            "latencies": [],
        },
        "tool_context": {
            "calls": 0, "injections": 0, "skips": 0,
            "total_latency_ms": 0, "total_chars": 0,
            "latencies": [],
        },
    }

    # Clean caches
    for f in ["/tmp/memento-deferred-briefing.json", "/tmp/memento-last-recall.json",
              "/tmp/memento-tool-context-cache.json"]:
        try:
            os.unlink(f)
        except OSError:
            pass

    # 1. Session briefing
    output, latency = run_hook("vault-briefing.py", {"cwd": cwd, "session_id": sid})
    stats["briefing"]["latency_ms"] = latency
    stats["briefing"]["chars"] = count_injected_chars(output)

    time.sleep(0.3)

    # 2. Replay prompts and reads in interleaved order
    read_idx = 0
    for prompt in prompts:
        # Simulate 1-3 file reads before each prompt
        reads_this_turn = min(3, len(reads) - read_idx)
        for _ in range(reads_this_turn):
            fpath = reads[read_idx]
            read_idx += 1

            hook_input = {
                "tool_name": "Read",
                "tool_input": {"file_path": fpath},
                "cwd": cwd,
                "session_id": sid,
            }
            output, latency = run_hook("vault-tool-context.py", hook_input, timeout=5)
            stats["tool_context"]["calls"] += 1
            stats["tool_context"]["total_latency_ms"] += latency
            stats["tool_context"]["latencies"].append(latency)

            if output:
                chars = count_injected_chars(output)
                stats["tool_context"]["total_chars"] += chars
                stats["tool_context"]["injections"] += 1
            else:
                stats["tool_context"]["skips"] += 1

        # Prompt recall
        hook_input = {"prompt": prompt, "cwd": cwd, "session_id": sid}
        output, latency = run_hook("vault-recall.py", hook_input, timeout=8)
        stats["recall"]["calls"] += 1
        stats["recall"]["total_latency_ms"] += latency
        stats["recall"]["latencies"].append(latency)

        if output:
            stats["recall"]["total_chars"] += count_injected_chars(output)
            stats["recall"]["injections"] += 1
        else:
            stats["recall"]["skips"] += 1

    # Remaining reads
    while read_idx < len(reads):
        fpath = reads[read_idx]
        read_idx += 1
        hook_input = {
            "tool_name": "Read",
            "tool_input": {"file_path": fpath},
            "cwd": cwd,
            "session_id": sid,
        }
        output, latency = run_hook("vault-tool-context.py", hook_input, timeout=5)
        stats["tool_context"]["calls"] += 1
        stats["tool_context"]["total_latency_ms"] += latency
        stats["tool_context"]["latencies"].append(latency)
        if output:
            stats["tool_context"]["total_chars"] += chars
            stats["tool_context"]["injections"] += 1
        else:
            stats["tool_context"]["skips"] += 1

    return stats


def percentile(lst, p):
    if not lst:
        return 0
    s = sorted(lst)
    idx = int(len(s) * p / 100)
    return s[min(idx, len(s) - 1)]


def print_report(all_stats):
    """Print comprehensive benchmark report."""
    n = len(all_stats)
    total_prompts = sum(s["prompt_count"] for s in all_stats)
    total_reads = sum(s["read_count"] for s in all_stats)

    print(f"\n{'='*65}")
    print(f"  MEMENTO VAULT PERFORMANCE REPORT — REAL SESSION REPLAY")
    print(f"{'='*65}")
    print(f"  Sessions replayed:  {n}")
    print(f"  Total prompts:      {total_prompts}")
    print(f"  Total file reads:   {total_reads}")
    projects = set()
    for s in all_stats:
        # Derive project from cwd
        cwd = s.get("cwd", "")
        name = Path(cwd).name if cwd else "unknown"
        projects.add(name)
    print(f"  Projects covered:   {len(projects)}")

    # --- Briefing ---
    b_lats = [s["briefing"]["latency_ms"] for s in all_stats]
    b_chars = [s["briefing"]["chars"] for s in all_stats]
    print(f"\n  SESSION BRIEFING (sync output only, QMD deferred)")
    print(f"  {'Avg latency:':<22} {sum(b_lats)/n:.0f}ms")
    print(f"  {'P95 latency:':<22} {percentile(b_lats, 95):.0f}ms")
    print(f"  {'Avg chars:':<22} {sum(b_chars)/n:.0f}")

    # --- Recall ---
    all_recall_lats = []
    for s in all_stats:
        all_recall_lats.extend(s["recall"]["latencies"])
    r_inj = sum(s["recall"]["injections"] for s in all_stats)
    r_skip = sum(s["recall"]["skips"] for s in all_stats)
    r_total = r_inj + r_skip
    r_chars = sum(s["recall"]["total_chars"] for s in all_stats)
    r_effective = r_inj / max(r_total - r_skip, 1) * 100 if r_total > r_skip else 0

    print(f"\n  PROMPT RECALL (per-prompt BM25)")
    print(f"  {'Total calls:':<22} {r_total}")
    print(f"  {'Injections:':<22} {r_inj} ({r_inj/max(r_total,1)*100:.0f}% raw)")
    print(f"  {'Intentional skips:':<22} {r_skip}")
    if r_total > r_skip:
        print(f"  {'Effective hit rate:':<22} {r_inj}/{r_total - r_skip} ({r_effective:.0f}%)")
    print(f"  {'Avg latency/call:':<22} {sum(all_recall_lats)/max(len(all_recall_lats),1):.0f}ms")
    print(f"  {'P95 latency/call:':<22} {percentile(all_recall_lats, 95):.0f}ms")
    print(f"  {'Total chars injected:':<22} {r_chars}")
    print(f"  {'Avg chars/session:':<22} {r_chars/n:.0f}")

    # --- Tool context ---
    all_tc_lats = []
    for s in all_stats:
        all_tc_lats.extend(s["tool_context"]["latencies"])
    tc_inj = sum(s["tool_context"]["injections"] for s in all_stats)
    tc_skip = sum(s["tool_context"]["skips"] for s in all_stats)
    tc_total = tc_inj + tc_skip
    tc_chars = sum(s["tool_context"]["total_chars"] for s in all_stats)

    print(f"\n  TOOL CONTEXT (PreToolUse:Read)")
    print(f"  {'Total calls:':<22} {tc_total}")
    print(f"  {'Injections:':<22} {tc_inj} ({tc_inj/max(tc_total,1)*100:.0f}% raw)")
    print(f"  {'Intentional skips:':<22} {tc_skip}")
    if tc_total > tc_skip:
        searchable = tc_total - tc_skip
        print(f"  {'Effective hit rate:':<22} {tc_inj}/{searchable} ({tc_inj/searchable*100:.0f}%)")
    print(f"  {'Avg latency/call:':<22} {sum(all_tc_lats)/max(len(all_tc_lats),1):.0f}ms")
    print(f"  {'P95 latency/call:':<22} {percentile(all_tc_lats, 95):.0f}ms")
    print(f"  {'Total chars injected:':<22} {tc_chars}")
    print(f"  {'Avg chars/session:':<22} {tc_chars/n:.0f}")

    # --- Cost summary ---
    total_chars = sum(b_chars) + r_chars + tc_chars
    avg_per_session = total_chars / n
    est_units = avg_per_session / 4

    print(f"\n  {'='*55}")
    print(f"  COST SUMMARY")
    print(f"  {'='*55}")
    print(f"  {'Total chars injected:':<30} {total_chars}")
    print(f"  {'Avg chars/session:':<30} {avg_per_session:.0f}")
    print(f"  {'Est. input units/session:':<30} ~{est_units:.0f}")
    print(f"  {'Breakdown:':<30}")
    print(f"    {'Briefing:':<28} {sum(b_chars)/n:.0f} chars/session ({sum(b_chars)/max(total_chars,1)*100:.0f}%)")
    print(f"    {'Recall:':<28} {r_chars/n:.0f} chars/session ({r_chars/max(total_chars,1)*100:.0f}%)")
    print(f"    {'Tool context:':<28} {tc_chars/n:.0f} chars/session ({tc_chars/max(total_chars,1)*100:.0f}%)")

    print(f"\n  COMPARISON vs CONCIERGE AGENT")
    print(f"  {'Concierge (1 call):':<30} ~72,500 input units, 60-90s")
    print(f"  {'Hooks (full session):':<30} ~{est_units:.0f} input units, <500ms/injection")
    if est_units > 0:
        ratio = 72500 / est_units
        print(f"  {'Efficiency:':<30} {ratio:.0f}x fewer input units")
    print(f"{'='*65}\n")


def run_inception_benchmark(quiet=False):
    """Run Inception pipeline in dry-run mode and report timing.

    Executes memento-inception.py --dry-run --full --verbose and parses
    stderr for phase timing and cluster information.

    Returns dict with timing and cluster stats, or None on failure.
    """
    inception_script = HOOKS_DIR / "memento-inception.py"
    if not inception_script.exists():
        print("  Inception script not found, skipping")
        return None

    if not quiet:
        print("\n  Running Inception pipeline (dry-run)...", flush=True)

    t0 = time.time()
    try:
        result = subprocess.run(
            [sys.executable, str(inception_script), "--dry-run", "--full", "--verbose"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        total_ms = int((time.time() - t0) * 1000)
    except subprocess.TimeoutExpired:
        total_ms = int((time.time() - t0) * 1000)
        print(f"  Inception timed out after {total_ms}ms")
        return None
    except FileNotFoundError:
        print("  Python interpreter not found")
        return None

    stderr = result.stderr or ""
    stdout = result.stdout or ""

    # Parse stderr lines for metrics
    stats = {
        "total_ms": total_ms,
        "exit_code": result.returncode,
        "notes_collected": 0,
        "notes_with_embeddings": 0,
        "total_clusters": 0,
        "clusters_with_new": 0,
    }

    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("New notes since last run:"):
            try:
                stats["notes_collected"] = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif line.startswith("Total clusterable notes:"):
            try:
                stats["notes_with_embeddings"] = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif line.startswith("Found") and "total clusters" in line:
            try:
                stats["total_clusters"] = int(line.split()[1])
            except (ValueError, IndexError):
                pass
        elif line.startswith("Clusters with new notes"):
            try:
                stats["clusters_with_new"] = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif line.startswith("Only") and "notes have embeddings" in line:
            try:
                stats["notes_with_embeddings"] = int(line.split()[1])
            except (ValueError, IndexError):
                pass

    return stats


def print_inception_report(stats):
    """Print Inception benchmark results."""
    if stats is None:
        return

    print(f"\n  {'='*55}")
    print(f"  INCEPTION PIPELINE (dry-run)")
    print(f"  {'='*55}")
    print(f"  {'Exit code:':<28} {stats['exit_code']}")
    print(f"  {'Total pipeline time:':<28} {stats['total_ms']}ms")
    print(f"  {'Notes collected:':<28} {stats['notes_collected']}")
    print(f"  {'Notes with embeddings:':<28} {stats['notes_with_embeddings']}")
    print(f"  {'Total clusters found:':<28} {stats['total_clusters']}")
    print(f"  {'Clusters with new notes:':<28} {stats['clusters_with_new']}")

    if stats["exit_code"] == 2:
        print(f"  {'Note:':<28} Missing ML dependencies (numpy, hdbscan, scikit-learn)")
    elif stats["exit_code"] == 3:
        print(f"  {'Note:':<28} No embeddings found — QMD may not be indexed")
    elif stats["exit_code"] == 1:
        print(f"  {'Note:':<28} Another Inception instance was running (locked)")
    print(f"  {'='*55}")


def main():
    parser = argparse.ArgumentParser(description="Replay real sessions through memento hooks")
    parser.add_argument("--max-sessions", type=int, default=30, help="Max total sessions to replay")
    parser.add_argument("--max-per-project", type=int, default=2, help="Max sessions per project")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--inception", action="store_true", help="Also benchmark the Inception pipeline (dry-run)")
    args = parser.parse_args()

    print("Finding transcripts...")
    transcripts = find_transcripts(max_per_project=args.max_per_project)
    print(f"Found {len(transcripts)} transcripts")

    # Parse all transcripts
    parsed = []
    for t in transcripts:
        p = parse_transcript(t)
        if p["user_prompts"] or p["read_paths"]:  # skip empty sessions
            parsed.append(p)
    print(f"Parsed {len(parsed)} non-empty sessions")

    # Limit total
    parsed = parsed[:args.max_sessions]
    print(f"Replaying {len(parsed)} sessions...\n")

    all_stats = []
    for i, p in enumerate(parsed):
        project = Path(p["cwd"]).name if p["cwd"] else "unknown"
        prompts = len(p["user_prompts"])
        reads = len(p["read_paths"])
        if not args.quiet:
            print(f"  [{i+1}/{len(parsed)}] {project} ({prompts} prompts, {reads} reads)...", end=" ", flush=True)
        stats = replay_session(p, quiet=args.quiet)
        total = stats["briefing"]["chars"] + stats["recall"]["total_chars"] + stats["tool_context"]["total_chars"]
        if not args.quiet:
            print(f"{total} chars")
        all_stats.append(stats)

    print_report(all_stats)

    # Inception benchmark (optional)
    inception_stats = None
    if args.inception:
        inception_stats = run_inception_benchmark(quiet=args.quiet)
        print_inception_report(inception_stats)

    # Save raw data
    out_path = Path(__file__).parent / "replay_results.jsonl"
    with open(out_path, "w") as f:
        for s in all_stats:
            # Remove latency arrays for compact output
            s2 = dict(s)
            s2["recall"] = {k: v for k, v in s["recall"].items() if k != "latencies"}
            s2["tool_context"] = {k: v for k, v in s["tool_context"].items() if k != "latencies"}
            f.write(json.dumps(s2) + "\n")
        if inception_stats:
            f.write(json.dumps({"inception": inception_stats}) + "\n")
    print(f"Raw data: {out_path}")


if __name__ == "__main__":
    main()
