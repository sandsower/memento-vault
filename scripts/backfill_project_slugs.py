#!/usr/bin/env python3
"""One-shot backfill: normalize `project` frontmatter to stable slugs (MEM-164).

Historically the `project` field stored whatever the writer had on hand:
absolute cwd paths from multiple machines (`/Users/...`, `/home/vic/...`),
per-ticket worktree paths, bare branch names, and occasionally a real slug.
Retrieval now compares project *slugs*, so this script rewrites existing notes
to the same shape the write path produces today:

  - path-like values      -> repo-name slug via ``repo_slug_from_path`` (git
                             toplevel/common-dir basename when the path still
                             exists locally, else the path basename); the old
                             raw value is preserved verbatim in a new
                             ``project_path`` field
  - bare branch names     -> left as-is and reported (a branch name cannot be
                             safely mapped back to a repo)
  - already-slug values   -> normalized (lowercase, dashes)

Tags are normalized with the same write-time function (lowercase, trim,
spaces->dashes, config ``tag_aliases`` map) and the distinct-tag count is
reported before/after.

Dry-run by default: prints a summary table (old value -> slug, count) and
touches nothing. Pass ``--apply`` to rewrite notes via the store's atomic
write helper. Unknown frontmatter keys round-trip verbatim because only the
``project:``/``project_path:``/``tags:`` lines are touched.

Usage:
    python3 scripts/backfill_project_slugs.py [--vault PATH] [--apply]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memento.config import get_vault, repo_slug_from_path, slugify  # noqa: E402
from memento.store import _normalize_tags, _write_text_atomic, split_frontmatter  # noqa: E402

# Bare branch names seen in legacy `project` fields. These cannot be mapped
# back to a repo mechanically, so they are reported but never rewritten.
BRANCH_NAME_VALUES = {"main", "master", "develop", "trunk", "head"}

_KEY_LINE_RE = re.compile(r"^([A-Za-z0-9_-]+):(.*)$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_tags_value(raw: str) -> list[str] | None:
    """Parse a flow-style tags value (`["a", "b"]` or `[a, b]`), else None."""
    raw = raw.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except ValueError:
        pass
    inner = raw[1:-1].strip()
    if not inner:
        return []
    return [_unquote(part) for part in inner.split(",")]


def classify_project_value(value: str) -> tuple[str, str | None]:
    """Return ``(category, new_slug_or_none)`` for a raw `project` value.

    Categories: ``path`` (rewritten to repo slug + project_path), ``branch``
    (left as-is, reported), ``slug`` (normalized in place), ``unchanged``.
    """
    if "/" in value or "\\" in value:
        return "path", repo_slug_from_path(value)
    if value.lower() in BRANCH_NAME_VALUES:
        return "branch", None
    normalized = slugify(value)
    if normalized and normalized != value:
        return "slug", normalized
    return "unchanged", None


def process_note(text: str) -> dict:
    """Compute the MEM-164 rewrite for one note's text.

    Returns a dict with keys: ``changed`` (bool), ``new_text``, ``category``,
    ``old_project``, ``new_project``, ``tags_before``, ``tags_after``.
    """
    result = {
        "changed": False,
        "new_text": text,
        "category": None,
        "old_project": None,
        "new_project": None,
        "tags_before": [],
        "tags_after": [],
    }
    frontmatter, body = split_frontmatter(text)
    if not frontmatter:
        return result

    lines = frontmatter.splitlines()
    has_project_path = any(line.startswith("project_path:") for line in lines)
    changed = False
    new_lines: list[str] = []

    for line in lines:
        match = _KEY_LINE_RE.match(line)
        key = match.group(1) if match else None

        if key == "project":
            old_value = _unquote(match.group(2))
            result["old_project"] = old_value
            if not old_value:
                new_lines.append(line)
                continue
            category, new_slug = classify_project_value(old_value)
            result["category"] = category
            if new_slug and new_slug != old_value:
                result["new_project"] = new_slug
                new_lines.append(f"project: {new_slug}")
                if category == "path" and not has_project_path:
                    new_lines.append(f"project_path: {old_value}")
                changed = True
            else:
                new_lines.append(line)
            continue

        if key == "tags":
            tags = _parse_tags_value(match.group(2))
            if tags is None:
                new_lines.append(line)
                continue
            result["tags_before"] = tags
            normalized = _normalize_tags(tags)
            result["tags_after"] = normalized
            if normalized != tags:
                new_lines.append(f"tags: {json.dumps(normalized, ensure_ascii=False)}")
                changed = True
            else:
                new_lines.append(line)
            continue

        new_lines.append(line)

    if changed:
        result["changed"] = True
        result["new_text"] = "---\n" + "\n".join(new_lines) + "\n---\n" + body
    return result


def run(vault: Path, apply: bool) -> int:
    notes_dir = vault / "notes"
    if not notes_dir.is_dir():
        print(f"error: notes directory not found at {notes_dir}", file=sys.stderr)
        return 1

    scanned = 0
    rewritten = 0
    mapping: Counter[tuple[str, str]] = Counter()
    branch_values: Counter[str] = Counter()
    tags_before: set[str] = set()
    tags_after: set[str] = set()

    for note_path in sorted(notes_dir.glob("*.md")):
        try:
            text = note_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"warning: skipping unreadable note {note_path.name}: {exc}", file=sys.stderr)
            continue
        scanned += 1

        outcome = process_note(text)
        tags_before.update(outcome["tags_before"])
        tags_after.update(outcome["tags_after"])

        if outcome["category"] == "branch":
            branch_values[outcome["old_project"]] += 1
        if outcome["new_project"]:
            mapping[(outcome["old_project"], outcome["new_project"])] += 1

        if outcome["changed"]:
            rewritten += 1
            if apply:
                _write_text_atomic(note_path, outcome["new_text"])

    mode = "APPLIED" if apply else "DRY-RUN (pass --apply to write)"
    print(f"MEM-164 project/tag backfill - {mode}")
    print(f"vault: {vault}")
    print(f"notes scanned: {scanned}; notes needing rewrite: {rewritten}")

    if mapping:
        print("\nproject value -> slug (count):")
        width = min(72, max(len(old) for (old, _new) in mapping))
        for (old, new), count in sorted(mapping.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {old[:72]:<{width}} -> {new}  ({count})")
    else:
        print("\nno project values need rewriting")

    if branch_values:
        print("\nbare branch names left as-is (cannot be safely inferred):")
        for value, count in sorted(branch_values.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {value}  ({count})")

    print(f"\ndistinct tags: {len(tags_before)} before -> {len(tags_after)} after")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault", type=Path, default=None, help="vault path (default: configured vault)")
    parser.add_argument("--apply", action="store_true", help="rewrite notes (default is a dry-run report)")
    args = parser.parse_args(argv)

    vault = args.vault if args.vault is not None else get_vault()
    return run(vault, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
