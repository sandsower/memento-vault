---
name: inception
description: Run or preview Memento Vault consolidation. Use when the user asks to consolidate, find patterns, run inception, preview clusters, or synthesize cross-session themes.
---

# Inception

Run Memento Vault consolidation to find patterns across notes.

## Process

1. Determine mode:
   - Default: incremental run
   - Dry run: user says preview, dry run, or what would be found
   - Full run: user says full, all notes, rescan, or backfill

2. Prefer the installed local script when available:

```bash
python3 ~/.claude/hooks/memento-inception.py --verbose [--dry-run] [--full]
```

3. If the script is unavailable, explain that Inception requires a local Memento install with the consolidation hook.

4. Interpret common outcomes:
   - Lock held: another run is active
   - Missing dependencies: install `numpy hdbscan scikit-learn`
   - No embeddings: reindex and embed the vault
   - Config error: inspect `memento.yml`

5. Report clusters found, notes written, and whether it was a dry run.

## Rules

- Do not write pattern notes during dry runs.
- Do not hide dependency or indexing failures.
- If pattern notes are written, suggest reviewing the new notes.
