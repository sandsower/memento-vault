#!/usr/bin/env python3
"""Check that documented frontmatter schema matches implemented writers.

This is intentionally stdlib-only so it can run in local hooks and CI without
requiring PyYAML or the optional Inception dependencies. It derives field names
from representative write fixtures and from the Inception writer source, then
compares them with docs/frontmatter-schema.md.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DOC_PATH = REPO_ROOT / "docs" / "frontmatter-schema.md"
INCEPTION_HOOK_PATH = REPO_ROOT / "hooks" / "memento-inception.py"
WRITER_SOURCE_PATHS = (
    REPO_ROOT / "memento" / "store.py",
    REPO_ROOT / "memento" / "mcp_server.py",
    REPO_ROOT / "memento" / "pi_bridge.py",
    REPO_ROOT / "hooks" / "memento-triage.py",
)

# Current compatibility/documentation-only source values. These are accepted in
# existing vaults but are not emitted by current durable-note writers.
LEGACY_DOCUMENTED_SOURCES = {"manual"}


@dataclass(frozen=True)
class SchemaExpectation:
    fields: set[str]
    field_types: dict[str, str]
    note_types: set[str]
    sources: set[str]


MANAGED_FIELD_TYPES = {
    "title": "string",
    "type": "enum",
    "tags": "list",
    "source": "enum-ish string",
    "origin": "string",
    "certainty": "int 1-5",
    "validity-context": "string",
    "supersedes": "wikilink or title",
    "synthesized_from": "list",
    "project": "string",
    "branch": "string",
    "date": "datetime",
    "session_id": "uuid/string",
    "repo_slug": "string",
}


def _frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("fixture does not start with YAML frontmatter")
    for idx, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "\n".join(lines[1:idx])
    raise ValueError("fixture frontmatter is unterminated")


def _frontmatter_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for line in _frontmatter(text).splitlines():
        if not line or line.startswith(" ") or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):", line)
        if match:
            keys.add(match.group(1))
    return keys


def _frontmatter_value(text: str, key: str) -> str | None:
    for line in _frontmatter(text).splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return None


def _write_note_fixture(vault: Path, *, note_type: str = "discovery", source: str = "session") -> str:
    from memento.store import write_note

    path = write_note(
        vault,
        title=f"Schema fixture {source} {note_type}",
        body="Fixture body.",
        note_type=note_type,
        tags=["schema", source],
        certainty=3,
        source=source,
        origin=f"fixture:{source}",
        validity_context="schema checker fixture",
        supersedes="[[older-schema-fixture]]",
        project="/tmp/memento-vault",
        branch="schema-checker",
        session_id=f"session-{source}",
    )
    return path.read_text(encoding="utf-8")


def _daily_snapshot_fixtures(vault: Path) -> list[str]:
    from memento.store import write_daily_snapshot

    first = write_daily_snapshot(
        vault,
        date="2026-06-29",
        repo_slug="memento-vault",
        content="Daily fixture body.",
    )
    if "error" in first:
        raise RuntimeError(first["error"])
    second = write_daily_snapshot(
        vault,
        date="2026-06-29",
        repo_slug="memento-vault",
        content="Daily supersede fixture body.",
        supersede=True,
    )
    if "error" in second:
        raise RuntimeError(second["error"])
    return [
        (vault / first["path"]).read_text(encoding="utf-8"),
        (vault / second["path"]).read_text(encoding="utf-8"),
    ]


def _inception_writer_frontmatter() -> str:
    source = INCEPTION_HOOK_PATH.read_text(encoding="utf-8")
    match = re.search(r'content = f"""---\n(?P<frontmatter>.*?)\n---', source, re.DOTALL)
    if not match:
        raise RuntimeError("could not locate Inception pattern-note frontmatter template")
    return f"---\n{match.group('frontmatter')}\n---\n"


def _string_constant(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _function_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def implemented_atomic_sources(source_paths: tuple[Path, ...] | None = None) -> set[str]:
    """Return source values implemented by ordinary atomic-note write paths.

    Sources are derived from code, not copied into this checker: function
    defaults on the shared store contract and literal `source=` arguments at
    writer/call-site boundaries. Variant writers with custom frontmatter
    (`write_daily_snapshot` and Inception) are covered by generated/template
    fixtures in `expected_schema()`.
    """
    source_paths = WRITER_SOURCE_PATHS if source_paths is None else source_paths
    sources: set[str] = set()
    functions_with_source_default = {"normalize_note_contract", "write_note"}
    calls_with_source_keyword = {"normalize_note_contract", "write_note"}

    for path in source_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions_with_source_default:
                positional_args = node.args.posonlyargs + node.args.args
                defaults = [None] * (len(positional_args) - len(node.args.defaults)) + list(node.args.defaults)
                for arg, default in zip(positional_args, defaults):
                    if arg.arg == "source":
                        value = _string_constant(default)
                        if value:
                            sources.add(value)

                for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                    if arg.arg == "source":
                        value = _string_constant(default)
                        if value:
                            sources.add(value)

            if isinstance(node, ast.Call) and _function_name(node.func) in calls_with_source_keyword:
                for keyword in node.keywords:
                    if keyword.arg == "source":
                        value = _string_constant(keyword.value)
                        if value:
                            sources.add(value)

    if not sources:
        raise RuntimeError("could not derive any atomic-note source values from writer implementations")
    return sources


def expected_schema() -> SchemaExpectation:
    fields: set[str] = set()
    sources: set[str] = set()
    note_types: set[str] = set()

    with tempfile.TemporaryDirectory() as tempdir:
        vault = Path(tempdir)
        for source in sorted(implemented_atomic_sources()):
            text = _write_note_fixture(vault, source=source)
            fields.update(_frontmatter_keys(text))
            source_value = _frontmatter_value(text, "source")
            if source_value:
                sources.add(source_value)

        for text in _daily_snapshot_fixtures(vault):
            fields.update(_frontmatter_keys(text))
            source_value = _frontmatter_value(text, "source")
            if source_value:
                sources.add(source_value)
            note_type = _frontmatter_value(text, "type")
            if note_type:
                note_types.add(note_type)

    inception_text = _inception_writer_frontmatter()
    fields.update(_frontmatter_keys(inception_text))
    inception_source = _frontmatter_value(inception_text, "source")
    if inception_source:
        sources.add(inception_source)
    inception_type = _frontmatter_value(inception_text, "type")
    if inception_type:
        note_types.add(inception_type)

    from memento.store import _CANONICAL_NOTE_TYPES

    note_types.update(_CANONICAL_NOTE_TYPES)

    missing_field_types = fields - set(MANAGED_FIELD_TYPES)
    if missing_field_types:
        raise RuntimeError("managed field type expectation missing for: " + ", ".join(sorted(missing_field_types)))
    field_types = {field: MANAGED_FIELD_TYPES[field] for field in fields}

    return SchemaExpectation(fields=fields, field_types=field_types, note_types=note_types, sources=sources)


def _section(markdown: str, heading: str) -> str:
    pattern = re.compile(rf"^## {re.escape(heading)}\s*$", re.MULTILINE)
    match = pattern.search(markdown)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"^## ", markdown[start:], re.MULTILINE)
    end = len(markdown) if next_heading is None else start + next_heading.start()
    return markdown[start:end]


def _table_rows(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        columns = [part.strip() for part in line.strip("|").split("|")]
        if not columns or columns[0].lower() in {"field", "type", "source", "writer / variant"}:
            continue
        if set(columns[0]) <= {"-", ":"}:
            continue
        rows.append(columns)
    return rows


def _cell_value(cell: str) -> str:
    match = re.search(r"`([^`]+)`", cell)
    return match.group(1) if match else cell


def _table_first_column_values(section: str) -> set[str]:
    return {_cell_value(row[0]) for row in _table_rows(section)}


def _field_table_types(section: str) -> dict[str, str]:
    field_types: dict[str, str] = {}
    for row in _table_rows(section):
        if len(row) >= 2:
            field_types[_cell_value(row[0])] = row[1]
    return field_types


def documented_schema(doc_path: Path = DOC_PATH) -> SchemaExpectation:
    markdown = doc_path.read_text(encoding="utf-8")
    field_sections = [
        _section(markdown, "Required fields"),
        _section(markdown, "Optional fields"),
        _section(markdown, "Variant-specific fields"),
    ]
    fields: set[str] = set()
    field_types: dict[str, str] = {}
    for section in field_sections:
        fields.update(_table_first_column_values(section))
        field_types.update(_field_table_types(section))

    return SchemaExpectation(
        fields=fields,
        field_types=field_types,
        note_types=_table_first_column_values(_section(markdown, "Note types")),
        sources=_table_first_column_values(_section(markdown, "Source values")),
    )


def compare(expected: SchemaExpectation, documented: SchemaExpectation) -> list[str]:
    errors: list[str] = []

    missing_fields = expected.fields - documented.fields
    extra_fields = documented.fields - expected.fields
    if missing_fields:
        errors.append(f"docs/frontmatter-schema.md is missing field(s): {', '.join(sorted(missing_fields))}")
    if extra_fields:
        errors.append(f"docs/frontmatter-schema.md documents non-managed field(s): {', '.join(sorted(extra_fields))}")

    type_mismatches = []
    for field in sorted(expected.fields & documented.fields):
        expected_type = expected.field_types.get(field)
        documented_type = documented.field_types.get(field)
        if expected_type != documented_type:
            type_mismatches.append(f"{field} (expected {expected_type}, documented {documented_type})")
    if type_mismatches:
        errors.append("docs/frontmatter-schema.md has field type drift: " + "; ".join(type_mismatches))

    missing_types = expected.note_types - documented.note_types
    extra_types = documented.note_types - expected.note_types
    if missing_types:
        errors.append(f"docs/frontmatter-schema.md is missing note type(s): {', '.join(sorted(missing_types))}")
    if extra_types:
        errors.append(
            f"docs/frontmatter-schema.md documents unsupported note type(s): {', '.join(sorted(extra_types))}"
        )

    expected_sources = expected.sources | LEGACY_DOCUMENTED_SOURCES
    missing_sources = expected_sources - documented.sources
    extra_sources = documented.sources - expected_sources
    if missing_sources:
        errors.append(f"docs/frontmatter-schema.md is missing source value(s): {', '.join(sorted(missing_sources))}")
    if extra_sources:
        errors.append(
            f"docs/frontmatter-schema.md documents unsupported source value(s): {', '.join(sorted(extra_sources))}"
        )

    return errors


def run_check(doc_path: Path = DOC_PATH) -> list[str]:
    return compare(expected_schema(), documented_schema(doc_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable check result")
    args = parser.parse_args(argv)

    expected = expected_schema()
    documented = documented_schema()
    errors = compare(expected, documented)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not errors,
                    "errors": errors,
                    "expected": {
                        "fields": sorted(expected.fields),
                        "field_types": {field: expected.field_types[field] for field in sorted(expected.field_types)},
                        "note_types": sorted(expected.note_types),
                        "sources": sorted(expected.sources | LEGACY_DOCUMENTED_SOURCES),
                    },
                    "documented": {
                        "fields": sorted(documented.fields),
                        "field_types": {
                            field: documented.field_types[field] for field in sorted(documented.field_types)
                        },
                        "note_types": sorted(documented.note_types),
                        "sources": sorted(documented.sources),
                    },
                },
                indent=2,
            )
        )
    elif errors:
        print("Frontmatter schema drift detected:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
    else:
        print("Frontmatter schema docs match implemented writers.")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
