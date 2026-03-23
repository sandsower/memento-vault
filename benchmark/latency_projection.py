#!/usr/bin/env python3
"""Project recall latency scaling by simulating larger vault sizes.

Creates synthetic notes in a temp vault, indexes with QMD, and measures
BM25 + CE pipeline latency at each size tier.

Usage:
    python benchmark/latency_projection.py
    python benchmark/latency_projection.py --max-notes 5000 --step 500
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))


def generate_synthetic_notes(vault_path, count, existing_count=0):
    """Generate synthetic vault notes with realistic frontmatter and content."""
    notes_dir = vault_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    # Topic pools for realistic content
    domains = ["redis", "auth", "billing", "caching", "frontend", "api", "database",
               "testing", "deployment", "monitoring", "search", "graphql", "websockets",
               "migrations", "permissions", "notifications", "scheduling", "logging"]
    types = ["decision", "discovery", "pattern", "bugfix", "tool"]
    projects = ["api-service", "frontend-app", "billing-service", "auth-gateway",
                "data-pipeline", "mobile-app", "admin-dashboard", "worker-service"]

    import random
    rng = random.Random(42)

    for i in range(existing_count, existing_count + count):
        domain = rng.choice(domains)
        domain2 = rng.choice(domains)
        note_type = rng.choice(types)
        project = rng.choice(projects)
        certainty = rng.randint(2, 5)

        title = f"{domain}-{note_type}-{i}"
        body = (
            f"The {domain} layer in {project} required changes to the {domain2} configuration. "
            f"This was discovered during a session working on the {project} {domain} module. "
            f"The fix involved updating the {domain2} settings to handle edge cases properly. "
            f"Related to the overall {domain} architecture decisions made earlier."
        )

        content = f"""---
title: {title}
type: {note_type}
tags: [{domain}, {domain2}, {project}]
source: session
certainty: {certainty}
project: /home/user/projects/{project}
date: 2026-03-{rng.randint(1,22):02d}T{rng.randint(8,22):02d}:{rng.randint(0,59):02d}
---

{body}

## Related

"""
        (notes_dir / f"{title}.md").write_text(content)


def setup_qmd_collection(vault_path, collection_name):
    """Create a temporary QMD collection for the synthetic vault."""
    if not shutil.which("qmd"):
        print("QMD not installed, cannot run projection")
        sys.exit(1)

    # Index the collection
    subprocess.run(
        ["qmd", "update", "-c", collection_name, "--path", str(vault_path), "--pattern", "**/*.md"],
        capture_output=True, timeout=60,
    )
    subprocess.run(["qmd", "embed"], capture_output=True, timeout=120)


def measure_latency(collection_name, queries, semantic=False):
    """Measure QMD search latency for a set of queries."""
    latencies = []
    for query in queries:
        t0 = time.monotonic()
        cmd = ["qmd", "search", query, "-c", collection_name, "-n", "10", "--json"]
        if semantic:
            cmd = ["qmd", "vsearch", query, "-c", collection_name, "-n", "10", "--json"]
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
        except subprocess.TimeoutExpired:
            latencies.append(10000)
            continue
        latencies.append(int((time.monotonic() - t0) * 1000))
    return latencies


def run_projection(max_notes=3000, step=500):
    """Run the latency projection at increasing vault sizes."""
    test_queries = [
        "how does the cache invalidation work",
        "redis TTL configuration",
        "auth middleware decision",
        "what changed about billing",
        "last time we fixed the search",
        "deployment monitoring setup",
        "database migration strategy",
        "frontend testing patterns",
        "api rate limiting approach",
        "websocket connection handling",
    ]

    collection = "memento-bench"

    # Check current real vault size for reference
    from memento_utils import get_vault
    real_vault = get_vault()
    real_count = len(list((real_vault / "notes").glob("*.md"))) if (real_vault / "notes").exists() else 0

    print(f"Current vault: {real_count} notes")
    print(f"Projecting latency from {step} to {max_notes} notes (step {step})")
    print(f"Queries per tier: {len(test_queries)}")
    print()
    print(f"{'Notes':>8s}  {'BM25 avg':>10s}  {'BM25 p95':>10s}  {'Alert':>10s}")
    print(f"{'-----':>8s}  {'--------':>10s}  {'--------':>10s}  {'-----':>10s}")

    results = []

    with tempfile.TemporaryDirectory(prefix="memento-bench-") as tmpdir:
        vault_path = Path(tmpdir) / "vault"
        vault_path.mkdir()

        # Create QMD config for this temp collection
        qmd_config = Path.home() / ".config" / "qmd" / "index.yml"
        if qmd_config.exists():
            original_config = qmd_config.read_text()
        else:
            original_config = None

        total_generated = 0

        try:
            for target_count in range(step, max_notes + 1, step):
                # Generate notes incrementally
                to_add = target_count - total_generated
                generate_synthetic_notes(vault_path, to_add, existing_count=total_generated)
                total_generated = target_count

                # Re-index
                subprocess.run(
                    ["qmd", "update", "-c", collection, "--path", str(vault_path),
                     "--pattern", "**/*.md"],
                    capture_output=True, timeout=120,
                )

                # Measure BM25 latency
                lats = measure_latency(collection, test_queries, semantic=False)
                avg_lat = sum(lats) // len(lats) if lats else 0
                sorted_lats = sorted(lats)
                p95_lat = sorted_lats[int(len(sorted_lats) * 0.95)] if lats else 0

                alert = ""
                if avg_lat > 700:
                    alert = "!! ASYNC"
                elif avg_lat > 500:
                    alert = "~ watch"

                print(f"{target_count:>8d}  {avg_lat:>8d}ms  {p95_lat:>8d}ms  {alert:>10s}")

                results.append({
                    "notes": target_count,
                    "bm25_avg_ms": avg_lat,
                    "bm25_p95_ms": p95_lat,
                })

        finally:
            # Clean up the temp collection from QMD
            subprocess.run(
                ["qmd", "remove", "-c", collection],
                capture_output=True,
            )

    # Summary
    print()
    if results:
        threshold_hit = next((r for r in results if r["bm25_avg_ms"] > 700), None)
        if threshold_hit:
            print(f"Async threshold (700ms avg) hit at ~{threshold_hit['notes']} notes")
        else:
            print(f"Latency stays under 700ms avg through {max_notes} notes")

        # Write results for further analysis
        out_path = Path(__file__).parent / "latency_projection.json"
        with open(out_path, "w") as f:
            json.dump({"current_vault_size": real_count, "projections": results}, f, indent=2)
        print(f"Raw data: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Project recall latency scaling")
    parser.add_argument("--max-notes", type=int, default=3000)
    parser.add_argument("--step", type=int, default=500)
    args = parser.parse_args()

    run_projection(max_notes=args.max_notes, step=args.step)
