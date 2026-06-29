---
name: preserve
description: Archive artifact bundles in Memento Vault. Use when the user says preserve, archive, or save generated artifacts, or wants evidence bundles kept intact instead of turned into atomic notes.
---

# Preserve

Archive artifact bundles intact under `archive/`.

## When to use

- User says "preserve this", "archive this", "save these artifacts", or "move this to the vault archive"
- Generated handoff bundles, screenshots, logs, proofs, attachments, or other evidence packets
- Anything that should stay bundled rather than become atomic notes

## Process

1. Summarize what will be preserved and whether copy or move will be used.
2. Copy by default. Only move when the user explicitly requests a destructive move.
3. Prefer the `memento_preserve` MCP tool when available.
4. Preserve into `archive/<slug>/` with the source tree intact.
5. Write a manifest at `archive/<slug>/.memento/manifest.json` and a lightweight index note at `archive/<slug>/.memento/index.md`.
6. If a project can be detected, update the relevant project index with a link to the bundle index note.
7. Include original source path, title, description, tags, cwd, branch, and session id when known.
8. Warn if files look sensitive; do not silently redact the preserved bundle itself.
9. After preserving, report the archive path and manifest path.

## Rules

- Do not create atomic notes unless the user also asks for a summary note.
- Do not flatten the bundle into a prose summary.
- Never move by default.
