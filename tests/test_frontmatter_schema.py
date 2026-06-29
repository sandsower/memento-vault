"""Tests for the frontmatter schema/docs drift checker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_checker():
    path = Path(__file__).parent.parent / "scripts" / "check_frontmatter_schema.py"
    spec = importlib.util.spec_from_file_location("check_frontmatter_schema", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_frontmatter_schema_docs_match_implemented_writers():
    checker = _load_checker()

    assert checker.run_check() == []


def _write_doc_copy(tmp_path, content: str) -> Path:
    doc_copy = tmp_path / "frontmatter-schema.md"
    doc_copy.write_text(content, encoding="utf-8")
    return doc_copy


def test_frontmatter_schema_derives_atomic_sources_from_writer_implementations(tmp_path):
    checker = _load_checker()
    writer = tmp_path / "writer.py"
    writer.write_text(
        """
def write_note(source="session"):
    pass


def capture():
    write_note(source="custom-capture")
""".strip(),
        encoding="utf-8",
    )

    assert checker.implemented_atomic_sources((writer,)) == {"session", "custom-capture"}


def test_frontmatter_schema_checker_detects_missing_documented_field(tmp_path):
    checker = _load_checker()
    original = checker.DOC_PATH.read_text(encoding="utf-8")
    doc_copy = _write_doc_copy(
        tmp_path,
        original.replace(
            "| `repo_slug` | string | Daily snapshot repository identifier; emitted by `write_daily_snapshot` |\n", ""
        ),
    )

    errors = checker.run_check(doc_copy)

    assert any("repo_slug" in error for error in errors)


def test_frontmatter_schema_checker_detects_documented_field_type_drift(tmp_path):
    checker = _load_checker()
    original = checker.DOC_PATH.read_text(encoding="utf-8")
    doc_copy = _write_doc_copy(
        tmp_path,
        original.replace(
            "| `repo_slug` | string | Daily snapshot repository identifier; emitted by `write_daily_snapshot` |\n",
            "| `repo_slug` | int | Daily snapshot repository identifier; emitted by `write_daily_snapshot` |\n",
        ),
    )

    errors = checker.run_check(doc_copy)

    assert any("repo_slug" in error and "field type drift" in error for error in errors)


def test_frontmatter_schema_checker_detects_missing_documented_note_type(tmp_path):
    checker = _load_checker()
    original = checker.DOC_PATH.read_text(encoding="utf-8")
    doc_copy = _write_doc_copy(
        tmp_path,
        original.replace(
            "| `architecture` | System design, integration boundaries, or durable architectural context |\n", ""
        ),
    )

    errors = checker.run_check(doc_copy)

    assert any("architecture" in error for error in errors)


def test_frontmatter_schema_checker_detects_missing_documented_source(tmp_path):
    checker = _load_checker()
    original = checker.DOC_PATH.read_text(encoding="utf-8")
    doc_copy = _write_doc_copy(
        tmp_path,
        original.replace("| `mcp-capture` | `memento_capture` session-summary note writer |\n", ""),
    )

    errors = checker.run_check(doc_copy)

    assert any("mcp-capture" in error for error in errors)
