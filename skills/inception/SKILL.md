---
name: inception
description: Run the Inception — background consolidation agent that clusters vault notes by embedding similarity and produces pattern/synthesis notes. Use PROACTIVELY when the user says "inception", "consolidate", "find patterns", "dream", "run inception", "pattern detection", or invokes /inception. Also use when the user asks about cross-session themes or recurring insights in their vault.
---

# Inception

Runs the Inception consolidation agent against the memento vault. It clusters notes by semantic similarity (HDBSCAN on QMD embeddings) and synthesizes pattern notes via the configured LLM backend.

## Process

1. Determine the run mode from the user's request:
   - **Default**: incremental run (only new notes since last run)
   - **Dry run**: if the user says "dry run", "preview", or "what would inception find" — add `--dry-run`
   - **Full**: if the user says "full", "all notes", "backfill", or "rescan" — add `--full`
   - Flags can combine: `--dry-run --full`

2. Run the Inception script:

```bash
python3 ~/.claude/hooks/inception.py --verbose [--dry-run] [--full] [--max-clusters N] 2>&1; echo "EXIT:$?"
```

3. Parse the exit code from the last line (`EXIT:N`) and respond:

| Exit | Meaning | Response |
|------|---------|----------|
| 0 | Success | Parse stderr output for cluster count and notes written. Report to user. |
| 1 | Lock held | "Another Inception instance is already running. Try again in a few minutes." |
| 2 | Missing deps | "Inception dependencies are missing. Install them with: `pip install numpy hdbscan scikit-learn`" |
| 3 | No embeddings | "No embeddings found in QMD. Make sure the vault is indexed: `qmd update -c memento && qmd embed`" |
| 5 | Config error | "Configuration error. Check your memento.yml for invalid Inception settings." |

4. For exit 0, summarize:
   - How many clusters were found
   - How many pattern notes were written (or "dry run — no notes written")
   - If notes were written, list their titles

5. If notes were written, suggest the user review them:
   - "You can find the new pattern notes in your vault's notes/ directory"
   - "Run `/inception --dry-run` next time to preview before writing"
