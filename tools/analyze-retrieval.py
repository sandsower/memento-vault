#!/usr/bin/env python3
"""Analyze the full memento pipeline from retrieval logs.

Covers triage decisions, retrieval (recall + multi-hop), and inception triggers.

Usage:
    python tools/analyze-retrieval.py
    python tools/analyze-retrieval.py --since 7   # last 7 days
    python tools/analyze-retrieval.py --since 30  # last month
"""

import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

LOG_PATH = Path.home() / ".config" / "memento-vault" / "retrieval.jsonl"


def load_logs(since_days=None):
    if not LOG_PATH.exists():
        print(f"No log file at {LOG_PATH}")
        sys.exit(1)

    cutoff = None
    if since_days:
        cutoff = (datetime.now() - timedelta(days=since_days)).isoformat()

    entries = []
    for line in LOG_PATH.read_text().splitlines():
        try:
            entry = json.loads(line)
            if cutoff and entry.get("ts", "") < cutoff:
                continue
            entries.append(entry)
        except json.JSONDecodeError:
            continue
    return entries


def pct(n, total):
    return f"{n/total*100:.0f}%" if total else "n/a"


def avg(lst):
    return sum(lst) // len(lst) if lst else 0


def p95(lst):
    if not lst:
        return 0
    s = sorted(lst)
    return s[int(len(s) * 0.95)]


def analyze_triage(entries):
    triage = [e for e in entries if e.get("hook") == "triage"]
    if not triage:
        print("  No triage entries yet.\n")
        return

    decisions = [e for e in triage if e.get("action") == "decision"]
    spawned = [e for e in decisions if e.get("agent_spawned")]
    skipped = [e for e in decisions if not e.get("agent_spawned")]

    projects = Counter(e.get("project", "unknown") for e in decisions)
    exchanges = [e.get("exchanges", 0) for e in decisions]

    print(f"  Sessions triaged: {len(decisions)}")
    print(f"  Agent spawned: {len(spawned)} ({pct(len(spawned), len(decisions))})")
    print(f"  Fleeting only: {len(skipped)} ({pct(len(skipped), len(decisions))})")
    if exchanges:
        print(f"  Exchanges: avg {avg(exchanges)}, max {max(exchanges)}")

    substantial_no_insight = [e for e in decisions if e.get("substantial") and not e.get("new_insight")]
    if substantial_no_insight:
        print(f"  Substantial but no new insight (delta-check): {len(substantial_no_insight)}")

    print(f"  Projects: {', '.join(f'{p} ({c})' for p, c in projects.most_common(5))}")
    print()


def analyze_recall(entries):
    recall = [e for e in entries if e.get("hook") == "recall"]
    if not recall:
        print("  No recall entries yet.\n")
        return

    inject = [e for e in recall if e.get("action") == "inject"]
    no_results = [e for e in recall if e.get("action") == "no-results"]
    dedup = [e for e in recall if e.get("action") == "dedup-skip"]

    pipelines = Counter(e.get("pipeline", "unknown") for e in inject)
    latencies = [e["latency_ms"] for e in inject if "latency_ms" in e]
    chars = [e.get("injected_chars", 0) for e in inject]

    print(f"  Total calls: {len(recall)}")
    print(f"  Injected: {len(inject)} ({pct(len(inject), len(recall))})")
    print(f"  No results: {len(no_results)}")
    print(f"  Dedup skipped: {len(dedup)}")
    if latencies:
        print(f"  Latency: avg {avg(latencies)}ms, p95 {p95(latencies)}ms")
    if chars:
        print(f"  Chars injected: avg {avg(chars)}, total {sum(chars)}")

    print(f"  Pipeline distribution:")
    for pipeline, count in pipelines.most_common():
        print(f"    {pipeline}: {count} ({pct(count, len(inject))})")

    # Multi-hop specific
    hop_gated = [e for e in inject if e.get("multi_hop_gate")]
    hop_fired = [e for e in inject if "+hop" in e.get("pipeline", "")]
    if hop_gated or hop_fired:
        hop_added = [e.get("multi_hop_added", 0) for e in hop_fired]
        print(f"  Multi-hop gate triggered: {len(hop_gated)}")
        print(f"  Multi-hop fired: {len(hop_fired)}")
        if hop_added:
            print(f"  New results per hop: avg {sum(hop_added)/len(hop_added):.1f}")
        if hop_fired:
            print(f"  Example hop queries:")
            for e in hop_fired[:3]:
                print(f"    [{e.get('multi_hop_added', 0)} new] {e.get('query', '')[:70]}")
    print()


def analyze_inception(entries):
    inception = [e for e in entries if e.get("hook") == "inception"]
    if not inception:
        print("  No inception entries yet.\n")
        return

    triggers = [e for e in inception if e.get("action") == "trigger"]
    skips = [e for e in inception if e.get("action") == "skip"]

    print(f"  Trigger checks: {len(inception)}")
    print(f"  Triggered: {len(triggers)}")
    print(f"  Skipped (below threshold): {len(skips)}")
    if triggers:
        for t in triggers[-3:]:
            print(f"    {t.get('ts', '?')} — {t.get('new_notes', '?')} new notes")
    if skips:
        skip_notes = [e.get("new_notes", 0) for e in skips]
        print(f"  Notes at skip: avg {avg(skip_notes)}, max {max(skip_notes)}")
    print()


def analyze_briefing(entries):
    briefing = [e for e in entries if e.get("hook") == "briefing"]
    if not briefing:
        return

    inject = [e for e in briefing if e.get("action") == "inject"]
    latencies = [e["latency_ms"] for e in inject if "latency_ms" in e]

    print(f"  Sessions briefed: {len(inject)}")
    if latencies:
        print(f"  Latency: avg {avg(latencies)}ms, p95 {p95(latencies)}ms")
    print()


def main():
    days = None
    if "--since" in sys.argv:
        idx = sys.argv.index("--since")
        if idx + 1 < len(sys.argv):
            days = int(sys.argv[idx + 1])

    entries = load_logs(since_days=days)

    period_start = entries[0].get("ts", "?")[:10] if entries else "?"
    period_end = entries[-1].get("ts", "?")[:10] if entries else "?"
    print(f"=== Memento pipeline analysis ===")
    print(f"Period: {period_start} to {period_end}")
    print(f"Total log entries: {len(entries)}")
    print()

    print("--- Triage ---")
    analyze_triage(entries)

    print("--- Retrieval (recall) ---")
    analyze_recall(entries)

    print("--- Briefing ---")
    analyze_briefing(entries)

    print("--- Inception ---")
    analyze_inception(entries)


if __name__ == "__main__":
    main()
