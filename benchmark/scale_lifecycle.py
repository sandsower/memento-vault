#!/usr/bin/env python3
"""Scale lifecycle benchmark: clone the real vault at 1x/5x/10x/50x sizes
and measure the full recall pipeline against each.

Uses QMD with temp collections for real BM25 + vsearch latency.

Usage:
    python benchmark/scale_lifecycle.py
    python benchmark/scale_lifecycle.py --multipliers 1,10,50
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))

from memento_utils import get_config, get_vault


class Timer:
    def __init__(self):
        self.ms = 0
    def __enter__(self):
        self._t = time.monotonic()
        return self
    def __exit__(self, *a):
        self.ms = int((time.monotonic() - self._t) * 1000)


def clone_and_scale_vault(real_vault, target_dir, multiplier):
    """Clone the real vault and scale it by duplicating + mutating notes."""
    notes_dir = target_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    # Copy all real notes first
    real_notes = list((real_vault / "notes").glob("*.md"))
    for note in real_notes:
        shutil.copy2(note, notes_dir / note.name)

    if multiplier <= 1:
        return len(real_notes)

    # Generate scaled copies with mutated content
    rng = random.Random(42)
    domains = ["redis", "auth", "billing", "caching", "frontend", "api", "database",
               "testing", "deployment", "monitoring", "search", "graphql", "websockets",
               "migrations", "permissions", "notifications", "scheduling", "logging",
               "observability", "feature-flags", "rate-limiting", "pagination", "i18n",
               "session-management", "file-upload", "background-jobs", "webhooks"]
    verbs = ["requires", "breaks-when", "fixed-by", "pattern-for", "decision-on",
             "discovered-in", "workaround-for", "approach-to", "gotcha-with", "tip-for"]
    projects = ["api-service", "frontend-app", "billing-service", "auth-gateway",
                "data-pipeline", "mobile-app", "admin-dashboard", "worker-service",
                "search-indexer", "notification-service", "analytics-engine", "cdn-proxy"]

    total = len(real_notes)
    target_total = len(real_notes) * multiplier

    while total < target_total:
        # Pick a real note as a template
        template = rng.choice(real_notes)
        text = template.read_text()

        # Mutate: change domain terms, project, date
        domain = rng.choice(domains)
        domain2 = rng.choice(domains)
        verb = rng.choice(verbs)
        project = rng.choice(projects)
        day = rng.randint(1, 28)
        hour = rng.randint(8, 22)

        # Replace title and add unique content
        stem = f"synth-{domain}-{verb}-{total}"
        new_body = (
            f"The {domain} configuration in {project} needed adjustment after the {domain2} "
            f"refactor. The root cause was a mismatch between the {domain} client settings "
            f"and the {domain2} server expectations. Fixed by updating the {domain} config "
            f"to use explicit {domain2} parameters instead of relying on defaults. "
            f"This affected the {project} {domain} module specifically."
        )

        content = f"""---
title: {domain} {verb} {domain2} in {project}
type: {rng.choice(['decision', 'discovery', 'pattern', 'bugfix'])}
tags: [{domain}, {domain2}, {project}]
source: session
certainty: {rng.randint(2, 5)}
project: /home/user/projects/{project}
date: 2026-03-{day:02d}T{hour:02d}:{rng.randint(0,59):02d}
---

{new_body}

## Related

"""
        (notes_dir / f"{stem}.md").write_text(content)
        total += 1

    return total


def index_collection(collection, vault_path):
    """Create and index a QMD collection for the test vault."""
    subprocess.run(
        ["qmd", "update", "-c", collection, "--path", str(vault_path), "--pattern", "**/*.md"],
        capture_output=True, timeout=300,
    )
    subprocess.run(["qmd", "embed"], capture_output=True, timeout=300)


def remove_collection(collection):
    """Remove a QMD collection."""
    subprocess.run(["qmd", "remove", "-c", collection], capture_output=True)


def measure_recall(collection, prompts, config_overrides=None):
    """Measure full recall pipeline latency for a set of prompts."""
    results = []

    for label, prompt in prompts:
        timings = {}

        # BM25 search
        with Timer() as t:
            cmd = ["qmd", "search", prompt, "-c", collection, "-n", "10", "--json"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            try:
                bm25_results = json.loads(proc.stdout) if proc.stdout.strip() else []
                if isinstance(bm25_results, dict):
                    bm25_results = bm25_results.get("results", [])
            except json.JSONDecodeError:
                bm25_results = []
        timings["bm25"] = t.ms
        top_score = bm25_results[0].get("score", 0) if bm25_results else 0

        # vsearch (vector)
        with Timer() as t:
            cmd = ["qmd", "vsearch", prompt, "-c", collection, "-n", "10", "--json"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            try:
                vec_results = json.loads(proc.stdout) if proc.stdout.strip() else []
                if isinstance(vec_results, dict):
                    vec_results = vec_results.get("results", [])
            except json.JSONDecodeError:
                vec_results = []
        timings["vsearch"] = t.ms

        # PRF simulation: second BM25 with expanded query
        with Timer() as t:
            expanded = prompt + " configuration settings"
            cmd = ["qmd", "search", expanded, "-c", collection, "-n", "10", "--json"]
            subprocess.run(cmd, capture_output=True, timeout=10)
        timings["prf"] = t.ms

        total = sum(timings.values())
        bm25_count = len(bm25_results)
        vec_count = len(vec_results)

        results.append({
            "label": label,
            "prompt": prompt[:60],
            "total_ms": total,
            "bm25_ms": timings["bm25"],
            "vsearch_ms": timings["vsearch"],
            "prf_ms": timings["prf"],
            "bm25_results": bm25_count,
            "vec_results": vec_count,
            "top_score": round(top_score, 3),
        })

    return results


def run(multipliers):
    if not shutil.which("qmd"):
        print("QMD not installed, cannot run scale benchmark")
        sys.exit(1)

    real_vault = get_vault()
    real_count = len(list((real_vault / "notes").glob("*.md")))
    collection = "memento-scale-bench"

    test_prompts = [
        ("cache", "How does the Redis cache invalidation work?"),
        ("auth", "What did we decide about the auth middleware?"),
        ("billing", "Fix the billing reconciliation bug"),
        ("frontend", "React component testing patterns"),
        ("search", "OpenSearch indexing configuration"),
        ("deploy", "Deployment monitoring and alerting setup"),
        ("perf", "Performance optimization for the API layer"),
        ("pattern", "Common error handling patterns across services"),
    ]

    print(f"Real vault: {real_count} notes")
    print(f"Multipliers: {multipliers}")
    print(f"Queries per tier: {len(test_prompts)}")
    print()
    print(f"{'Scale':>7s} {'Notes':>7s}  {'BM25 avg':>9s} {'BM25 p95':>9s} {'Vec avg':>9s} {'PRF avg':>9s} {'Total avg':>10s} {'Total p95':>10s}  Alert")
    print(f"{'-----':>7s} {'-----':>7s}  {'--------':>9s} {'--------':>9s} {'-------':>9s} {'-------':>9s} {'---------':>10s} {'---------':>10s}  -----")

    all_results = []

    for mult in multipliers:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="memento-scale-") as tmpdir:
            vault_path = Path(tmpdir) / "vault"

            # Clone and scale
            note_count = clone_and_scale_vault(real_vault, vault_path, mult)

            # Index
            index_collection(collection, vault_path)

            # Warm vsearch model
            subprocess.run(
                ["qmd", "vsearch", "warmup", "-c", collection, "-n", "1"],
                capture_output=True, timeout=30,
            )

            # Measure
            results = measure_recall(collection, test_prompts)

            # Aggregate
            bm25_lats = [r["bm25_ms"] for r in results]
            vec_lats = [r["vsearch_ms"] for r in results]
            prf_lats = [r["prf_ms"] for r in results]
            total_lats = [r["total_ms"] for r in results]

            def avg(lst): return sum(lst) // len(lst) if lst else 0
            def p95(lst):
                if not lst: return 0
                s = sorted(lst)
                return s[int(len(s) * 0.95)]

            alert = ""
            if avg(total_lats) > 2000:
                alert = "!! ASYNC NOW"
            elif avg(total_lats) > 1200:
                alert = "!! ASYNC"
            elif avg(total_lats) > 700:
                alert = "~ watch"

            print(f"{mult:>5}x {note_count:>7d}  {avg(bm25_lats):>7d}ms {p95(bm25_lats):>7d}ms {avg(vec_lats):>7d}ms {avg(prf_lats):>7d}ms {avg(total_lats):>8d}ms {p95(total_lats):>8d}ms  {alert}")

            all_results.append({
                "multiplier": mult,
                "notes": note_count,
                "bm25_avg": avg(bm25_lats),
                "bm25_p95": p95(bm25_lats),
                "vsearch_avg": avg(vec_lats),
                "prf_avg": avg(prf_lats),
                "total_avg": avg(total_lats),
                "total_p95": p95(total_lats),
                "per_query": results,
            })

            # Cleanup collection
            remove_collection(collection)

    # Summary
    print()
    threshold_hit = next((r for r in all_results if r["total_avg"] > 1200), None)
    if threshold_hit:
        print(f"Async recommended at ~{threshold_hit['notes']} notes ({threshold_hit['multiplier']}x current vault)")
    else:
        max_notes = all_results[-1]["notes"] if all_results else 0
        print(f"Full pipeline stays under 1200ms avg through {max_notes} notes")

    # Save results
    out_path = Path(__file__).parent / "scale_lifecycle_results.json"
    with open(out_path, "w") as f:
        json.dump({"real_vault_size": real_count, "results": all_results}, f, indent=2)
    print(f"Raw data: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--multipliers", default="1,5,10,50",
                        help="Comma-separated multipliers (default: 1,5,10,50)")
    args = parser.parse_args()
    mults = [int(x) for x in args.multipliers.split(",")]
    run(mults)
