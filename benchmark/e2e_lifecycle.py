#!/usr/bin/env python3
"""End-to-end lifecycle benchmark for the memento-vault pipeline.

Simulates a full session lifecycle: triage → notes → briefing → recall → inception.
Uses mock LLM with configurable latency to model best/typical/worst scenarios.

Usage:
    python benchmark/e2e_lifecycle.py
    python benchmark/e2e_lifecycle.py --scenario typical
    python benchmark/e2e_lifecycle.py --scenario all
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))

# Scenarios: (name, agent_delay_s, inception_delay_s)
SCENARIOS = {
    "best":    {"agent_delay": 0.01, "inception_delay": 0.01, "label": "Best case (mocked instant)"},
    "typical": {"agent_delay": 0.05, "inception_delay": 0.05, "label": "Typical (50ms mock LLM)"},
    "worst":   {"agent_delay": 0.10, "inception_delay": 0.10, "label": "Worst case (100ms mock LLM)"},
}

MOCK_AGENT_NOTES = [
    {
        "stem": "e2e-test-redis-cache-decision",
        "title": "Redis cache requires explicit TTL in cluster mode",
        "type": "decision",
        "tags": ["redis", "caching", "e2e-test"],
        "certainty": 3,
        "body": "Redis cluster mode does not propagate DEL commands across shards for keys with no TTL. Every cached key needs an explicit TTL.",
    },
    {
        "stem": "e2e-test-auth-middleware-pattern",
        "title": "Auth middleware guards all API endpoints with JWT",
        "type": "pattern",
        "tags": ["auth", "middleware", "e2e-test"],
        "certainty": 4,
        "body": "The auth middleware validates JWT on every request. Refresh tokens use a separate endpoint with its own rate limiter.",
    },
]

MOCK_TRANSCRIPT = {
    "type": "user",
    "message": {"content": "Fix the Redis cache invalidation bug in the billing service"},
    "cwd": "/home/user/projects/billing",
    "gitBranch": "fix/cache-bug",
}


class Timer:
    def __init__(self, label):
        self.label = label
        self.elapsed_ms = 0

    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *args):
        self.elapsed_ms = int((time.monotonic() - self.start) * 1000)


def create_test_vault(tmpdir):
    """Create a test vault with structure and some existing notes."""
    vault = Path(tmpdir) / "vault"
    for d in ("notes", "fleeting", "projects", "archive"):
        (vault / d).mkdir(parents=True)

    # Seed with a few notes so recall has something to find
    for i, note in enumerate(MOCK_AGENT_NOTES):
        content = f"""---
title: {note['title']}
type: {note['type']}
tags: [{', '.join(note['tags'])}]
source: session
certainty: {note['certainty']}
project: /home/user/projects/billing
branch: main
date: 2026-03-20T10:{i:02d}
---

{note['body']}

## Related

"""
        (vault / "notes" / f"{note['stem']}.md").write_text(content)

    return vault


def create_test_transcript(tmpdir):
    """Create a fake JSONL transcript."""
    transcript = Path(tmpdir) / "transcript.jsonl"
    lines = []
    # 20 exchanges to be "substantial"
    for i in range(20):
        lines.append(json.dumps({
            "type": "user",
            "message": {"content": f"Prompt {i}: fix the cache invalidation in billing service"},
            "cwd": "/home/user/projects/billing",
            "gitBranch": "fix/cache-bug",
        }))
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"Response {i}"}]},
        }))
    transcript.write_text("\n".join(lines))
    return transcript


def create_test_config(vault_path):
    """Create a config dict pointing at the test vault."""
    from memento_utils import DEFAULT_CONFIG
    config = dict(DEFAULT_CONFIG)
    config["vault_path"] = str(vault_path)
    config["auto_commit"] = False
    config["session_briefing"] = True
    config["prompt_recall"] = True
    config["tool_context"] = True
    config["multi_hop_enabled"] = True
    config["inception_enabled"] = True
    config["inception_threshold"] = 1  # low threshold so it triggers
    config["inception_parallel"] = 2
    config["qmd_collection"] = ""  # disable QMD for the test
    return config


def simulate_triage(vault, transcript_path, config, scenario):
    """Simulate the triage decision (without spawning a real agent)."""
    from memento_utils import detect_project, slugify

    results = {"steps": {}, "errors": []}

    # Step 1: Parse transcript
    with Timer("parse_transcript") as t:
        from memento_triage import parse_transcript
        meta = parse_transcript(str(transcript_path))
    results["steps"]["parse_transcript"] = t.elapsed_ms

    # Step 2: Write fleeting
    with Timer("write_fleeting") as t:
        fleeting_dir = vault / "fleeting"
        today = datetime.now().strftime("%Y-%m-%d")
        fleeting_file = fleeting_dir / f"{today}.md"
        with open(fleeting_file, "a") as f:
            f.write(f"- `e2e-test` {meta['cwd']} ({meta['exchange_count']} exchanges)\n")
    results["steps"]["write_fleeting"] = t.elapsed_ms

    # Step 3: Check substantiality
    with Timer("is_substantial") as t:
        from memento_triage import is_substantial
        substantial = is_substantial(meta)
    results["steps"]["is_substantial"] = t.elapsed_ms
    results["substantial"] = substantial

    # Step 4: Simulate agent writing notes (mock LLM with delay)
    if substantial:
        with Timer("agent_write_notes") as t:
            time.sleep(scenario["agent_delay"])
            # Write mock notes
            for note in MOCK_AGENT_NOTES:
                note_path = vault / "notes" / f"e2e-fresh-{note['stem']}.md"
                if not note_path.exists():
                    content = f"""---
title: Fresh - {note['title']}
type: {note['type']}
tags: [{', '.join(note['tags'])}]
source: session
certainty: {note['certainty']}
project: /home/user/projects/billing
branch: fix/cache-bug
date: {datetime.now().strftime('%Y-%m-%dT%H:%M')}
---

{note['body']}

## Related

"""
                    note_path.write_text(content)
        results["steps"]["agent_write_notes"] = t.elapsed_ms
        results["notes_written"] = len(MOCK_AGENT_NOTES)

    return results


def simulate_briefing(vault, config):
    """Simulate session briefing (sync part only, no QMD)."""
    results = {"steps": {}}

    with Timer("briefing_project_detect") as t:
        from memento_utils import detect_project
        slug, ticket = detect_project("/home/user/projects/billing", "fix/cache-bug")
    results["steps"]["project_detect"] = t.elapsed_ms
    results["project"] = slug

    with Timer("briefing_read_project_index") as t:
        project_file = vault / "projects" / f"{slug}.md"
        if project_file.exists():
            project_text = project_file.read_text()
            results["project_index_exists"] = True
        else:
            results["project_index_exists"] = False
    results["steps"]["read_project_index"] = t.elapsed_ms

    # Simulate note listing (no QMD, just glob)
    with Timer("briefing_glob_notes") as t:
        notes = list((vault / "notes").glob("*.md"))
    results["steps"]["glob_notes"] = t.elapsed_ms
    results["vault_note_count"] = len(notes)

    return results


def simulate_recall(vault, config, prompt):
    """Simulate prompt recall (without QMD, using grep fallback)."""
    results = {"steps": {}}

    with Timer("recall_skip_check") as t:
        from vault_recall import should_skip
        skipped = should_skip(prompt, config)
    results["steps"]["skip_check"] = t.elapsed_ms
    results["skipped"] = skipped

    if not skipped:
        with Timer("recall_multi_hop_gate") as t:
            from memento_utils import extract_wikilinks
            hop_needed = len(extract_wikilinks("See [[example-note]] for context.")) > 0
        results["steps"]["multi_hop_gate"] = t.elapsed_ms
        results["multi_hop_gate"] = hop_needed

        # Simulate BM25 search (grep-based fallback since no QMD)
        with Timer("recall_grep_search") as t:
            matches = []
            for note in (vault / "notes").glob("*.md"):
                text = note.read_text().lower()
                if any(term in text for term in prompt.lower().split()[:3]):
                    matches.append(note.stem)
        results["steps"]["grep_search"] = t.elapsed_ms
        results["matches"] = len(matches)

    return results


def simulate_inception(vault, config, scenario):
    """Simulate inception trigger and clustering (mock LLM)."""
    results = {"steps": {}}

    with Timer("inception_note_count") as t:
        notes_dir = vault / "notes"
        note_count = len(list(notes_dir.glob("*.md")))
    results["steps"]["note_count"] = t.elapsed_ms
    results["total_notes"] = note_count

    threshold = config.get("inception_threshold", 5)
    should_trigger = note_count >= threshold
    results["should_trigger"] = should_trigger

    if should_trigger:
        # Simulate clustering (no HDBSCAN, just group by tag overlap)
        with Timer("inception_clustering") as t:
            clusters = {}
            for note in notes_dir.glob("*.md"):
                text = note.read_text()
                if "redis" in text.lower():
                    clusters.setdefault("redis", []).append(note.stem)
                if "auth" in text.lower():
                    clusters.setdefault("auth", []).append(note.stem)
        results["steps"]["clustering"] = t.elapsed_ms
        results["clusters_found"] = len(clusters)

        # Simulate LLM synthesis (mock with delay)
        with Timer("inception_synthesis") as t:
            for cid, stems in clusters.items():
                time.sleep(scenario["inception_delay"])
        results["steps"]["synthesis"] = t.elapsed_ms

        # Simulate pre-reasoning
        with Timer("inception_pre_reason") as t:
            # Would generate query predictions and connection maps
            pass
        results["steps"]["pre_reason"] = t.elapsed_ms

    return results


def run_scenario(name, scenario):
    """Run a full lifecycle simulation for one scenario."""
    print(f"\n{'=' * 60}")
    print(f"  {scenario['label']}")
    print(f"{'=' * 60}")

    with tempfile.TemporaryDirectory(prefix="memento-e2e-") as tmpdir:
        vault = create_test_vault(tmpdir)
        transcript = create_test_transcript(tmpdir)
        config = create_test_config(vault)

        total_start = time.monotonic()
        all_results = {}

        # Phase 1: Triage (session just ended)
        print(f"\n  Phase 1: Triage")
        triage = simulate_triage(vault, transcript, config, scenario)
        all_results["triage"] = triage
        for step, ms in triage["steps"].items():
            print(f"    {step}: {ms}ms")
        print(f"    substantial: {triage.get('substantial', False)}")
        print(f"    notes written: {triage.get('notes_written', 0)}")

        # Phase 2: Briefing (new session starts)
        print(f"\n  Phase 2: Briefing")
        briefing = simulate_briefing(vault, config)
        all_results["briefing"] = briefing
        for step, ms in briefing["steps"].items():
            print(f"    {step}: {ms}ms")
        print(f"    vault notes: {briefing.get('vault_note_count', 0)}")

        # Phase 3: Recall (user types prompts)
        prompts = [
            "How does the Redis cache work in billing?",
            "What was the previous auth middleware approach?",
            "Fix the broken test",
            "yes",
        ]
        print(f"\n  Phase 3: Recall ({len(prompts)} prompts)")
        recall_total = 0
        for prompt in prompts:
            recall = simulate_recall(vault, config, prompt)
            for step, ms in recall["steps"].items():
                recall_total += ms
            skipped = recall.get("skipped", False)
            matches = recall.get("matches", 0)
            hop = recall.get("multi_hop_gate", False)
            status = "skipped" if skipped else f"{matches} matches{' +hop' if hop else ''}"
            print(f"    \"{prompt[:40]}...\" -> {status} ({sum(recall['steps'].values())}ms)")
        all_results["recall_total_ms"] = recall_total

        # Phase 4: Inception (post-session consolidation)
        print(f"\n  Phase 4: Inception")
        inception = simulate_inception(vault, config, scenario)
        all_results["inception"] = inception
        for step, ms in inception["steps"].items():
            print(f"    {step}: {ms}ms")
        print(f"    triggered: {inception.get('should_trigger', False)}")
        print(f"    clusters: {inception.get('clusters_found', 0)}")

        total_ms = int((time.monotonic() - total_start) * 1000)
        all_results["total_ms"] = total_ms

        # Summary
        triage_ms = sum(triage["steps"].values())
        briefing_ms = sum(briefing["steps"].values())
        inception_ms = sum(inception["steps"].values())

        print(f"\n  {'─' * 40}")
        print(f"  Total wall clock:  {total_ms}ms")
        print(f"  Triage:            {triage_ms}ms")
        print(f"  Briefing:          {briefing_ms}ms")
        print(f"  Recall (4 prompts):{recall_total}ms")
        print(f"  Inception:         {inception_ms}ms")

        # Validation checks
        print(f"\n  Validation:")
        checks = []

        # Check fleeting was written
        fleeting_files = list((vault / "fleeting").glob("*.md"))
        ok = len(fleeting_files) > 0
        checks.append(("fleeting note written", ok))

        # Check agent notes exist
        agent_notes = list((vault / "notes").glob("e2e-fresh-*.md"))
        ok = len(agent_notes) == len(MOCK_AGENT_NOTES)
        checks.append((f"agent wrote {len(MOCK_AGENT_NOTES)} notes", ok))

        # Check recall found matches for relevant prompt
        ok = not simulate_recall(vault, config, "Redis cache invalidation")["skipped"]
        checks.append(("recall searches relevant prompts", ok))

        # Check recall skips short prompts
        ok = simulate_recall(vault, config, "yes")["skipped"]
        checks.append(("recall skips short prompts", ok))

        # Check wikilink extraction works (multi-hop gate)
        from memento_utils import extract_wikilinks
        ok = len(extract_wikilinks("Related: [[some-note]]")) > 0
        checks.append(("multi-hop wikilink extraction works", ok))

        # Check inception triggers
        ok = inception.get("should_trigger", False)
        checks.append(("inception triggered", ok))

        all_passed = True
        for label, passed in checks:
            status = "PASS" if passed else "FAIL"
            if not passed:
                all_passed = False
            print(f"    [{status}] {label}")

        all_results["all_checks_passed"] = all_passed
        return all_results


def main():
    parser = argparse.ArgumentParser(description="E2E lifecycle benchmark")
    parser.add_argument("--scenario", choices=["best", "typical", "worst", "all"], default="all")
    args = parser.parse_args()

    # Import triage module (has hyphen, need importlib)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "memento_triage",
        str(Path(__file__).parent.parent / "hooks" / "memento-triage.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memento_triage"] = mod
    spec.loader.exec_module(mod)

    # Also import vault_recall
    spec2 = importlib.util.spec_from_file_location(
        "vault_recall",
        str(Path(__file__).parent.parent / "hooks" / "vault-recall.py"),
    )
    mod2 = importlib.util.module_from_spec(spec2)
    sys.modules["vault_recall"] = mod2
    spec2.loader.exec_module(mod2)

    print("=" * 60)
    print("  MEMENTO VAULT — END-TO-END LIFECYCLE BENCHMARK")
    print("=" * 60)

    scenarios = [args.scenario] if args.scenario != "all" else ["best", "typical", "worst"]
    all_results = {}

    for name in scenarios:
        result = run_scenario(name, SCENARIOS[name])
        all_results[name] = result

    # Final summary
    if len(scenarios) > 1:
        print(f"\n{'=' * 60}")
        print(f"  SUMMARY")
        print(f"{'=' * 60}")
        print(f"  {'Scenario':<12s}  {'Total':>8s}  {'Triage':>8s}  {'Brief':>8s}  {'Recall':>8s}  {'Incep':>8s}  {'Pass':>6s}")
        for name in scenarios:
            r = all_results[name]
            t = sum(r["triage"]["steps"].values())
            b = sum(r["briefing"]["steps"].values())
            rc = r["recall_total_ms"]
            i = sum(r["inception"]["steps"].values())
            ok = "YES" if r["all_checks_passed"] else "NO"
            print(f"  {name:<12s}  {r['total_ms']:>6d}ms  {t:>6d}ms  {b:>6d}ms  {rc:>6d}ms  {i:>6d}ms  {ok:>6s}")

    # Write results
    out_path = Path(__file__).parent / "e2e_lifecycle_results.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nRaw data: {out_path}")


if __name__ == "__main__":
    main()
