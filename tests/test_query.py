"""Tests for memento/query.py: the typed metadata filter core.

Covers build_metadata_filter (the shared predicate extracted for MEM-158 so
memento_search can reuse memento_query's filter semantics) and the
read_note_record path-lookup wrapper. query_notes itself is covered end to
end in tests/test_mcp_server.py::TestMementoQuery; the agreement test below
asserts query_notes and build_metadata_filter produce the same accepted set
for the same fixture and filters, since query_notes now delegates to
build_metadata_filter internally.
"""

from memento.query import (
    QueryValidationError,
    _iter_note_records,
    build_metadata_filter,
    query_notes,
    read_note_record,
)


def _write_note(vault, name, frontmatter, body="Body.\n"):
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(body)
    (vault / "notes" / f"{name}.md").write_text("\n".join(lines))


class TestBuildMetadataFilter:
    def test_matches_on_type(self):
        predicate, filters = build_metadata_filter(note_type="decision")
        assert filters["type"] == "decision"
        assert predicate({"type": "decision", "tags": []}) is True
        assert predicate({"type": "discovery", "tags": []}) is False

    def test_matches_on_tag_membership(self):
        predicate, _ = build_metadata_filter(tag="cache")
        assert predicate({"type": "", "tags": ["api", "cache"]}) is True
        assert predicate({"type": "", "tags": ["api"]}) is False

    def test_matches_on_certainty_range(self):
        predicate, _ = build_metadata_filter(certainty_min=3, certainty_max=4)
        assert predicate({"tags": [], "certainty": 3}) is True
        assert predicate({"tags": [], "certainty": 4}) is True
        assert predicate({"tags": [], "certainty": 2}) is False
        assert predicate({"tags": [], "certainty": 5}) is False
        assert predicate({"tags": [], "certainty": None}) is False

    def test_matches_on_date_range(self):
        predicate, _ = build_metadata_filter(date_start="2026-06-01", date_end="2026-06-30")
        assert predicate({"tags": [], "date": "2026-06-15T10:00"}) is True
        assert predicate({"tags": [], "date": "2026-05-31T23:59"}) is False
        assert predicate({"tags": [], "date": "2026-07-01T00:00"}) is False

    def test_matches_on_branch_and_session_id(self):
        predicate, _ = build_metadata_filter(branch="feat/x", session_id="sess-1")
        assert predicate({"tags": [], "branch": "feat/x", "session_id": "sess-1"}) is True
        assert predicate({"tags": [], "branch": "main", "session_id": "sess-1"}) is False
        assert predicate({"tags": [], "branch": "feat/x", "session_id": "sess-2"}) is False

    def test_matches_on_project_exact(self):
        predicate, _ = build_metadata_filter(project="/repo/api")
        assert predicate({"tags": [], "project": "/repo/api"}) is True
        assert predicate({"tags": [], "project": "api"}) is False

    def test_no_filters_matches_everything(self):
        predicate, filters = build_metadata_filter()
        assert all(value is None for key, value in filters.items() if key != "include_invalidated")
        assert filters["include_invalidated"] is False
        assert predicate({"tags": []}) is True

    def test_default_excludes_invalidated_notes(self):
        predicate, _ = build_metadata_filter()
        assert predicate({"tags": [], "invalidated_by": "newer-note"}) is False

    def test_include_invalidated_true_includes_invalidated_notes(self):
        predicate, filters = build_metadata_filter(include_invalidated=True)
        assert filters["include_invalidated"] is True
        assert predicate({"tags": [], "invalidated_by": "newer-note"}) is True

    def test_invalid_certainty_range_raises(self):
        try:
            build_metadata_filter(certainty_min=5, certainty_max=2)
        except QueryValidationError as exc:
            assert "certainty_min" in str(exc)
        else:
            raise AssertionError("expected QueryValidationError")

    def test_invalid_date_raises(self):
        try:
            build_metadata_filter(date_start="not a date")
        except QueryValidationError as exc:
            assert "date_start" in str(exc)
        else:
            raise AssertionError("expected QueryValidationError")


class TestReadNoteRecord:
    def test_reads_frontmatter_by_relative_path(self, tmp_vault):
        _write_note(tmp_vault, "alpha", {"title": "Alpha", "type": "decision", "certainty": 4})

        record = read_note_record(tmp_vault, "notes/alpha.md")

        assert record["path"] == "notes/alpha.md"
        assert record["type"] == "decision"
        assert record["certainty"] == 4

    def test_reads_frontmatter_by_absolute_path(self, tmp_vault):
        _write_note(tmp_vault, "alpha", {"title": "Alpha", "type": "decision"})

        record = read_note_record(tmp_vault, tmp_vault / "notes" / "alpha.md")

        assert record["path"] == "notes/alpha.md"

    def test_missing_file_returns_none(self, tmp_vault):
        assert read_note_record(tmp_vault, "notes/does-not-exist.md") is None


class TestQueryNotesAndFilterAgree:
    """query_notes now delegates to build_metadata_filter; assert they agree."""

    def test_same_fixture_same_filters_same_accepted_set(self, tmp_vault):
        _write_note(
            tmp_vault,
            "match-all",
            {
                "title": "Match all",
                "type": "decision",
                "tags": "[api, cache]",
                "certainty": 4,
                "date": "2026-06-10T09:30",
                "project": "/repo/api",
                "branch": "main",
                "session_id": "sess-1",
            },
        )
        _write_note(
            tmp_vault,
            "wrong-type",
            {
                "title": "Wrong type",
                "type": "discovery",
                "tags": "[api, cache]",
                "certainty": 4,
                "date": "2026-06-10T09:30",
                "project": "/repo/api",
                "branch": "main",
                "session_id": "sess-1",
            },
        )
        _write_note(
            tmp_vault,
            "low-certainty",
            {
                "title": "Low certainty",
                "type": "decision",
                "tags": "[api, cache]",
                "certainty": 2,
                "date": "2026-06-10T09:30",
                "project": "/repo/api",
                "branch": "main",
                "session_id": "sess-1",
            },
        )
        _write_note(
            tmp_vault,
            "out-of-range-date",
            {
                "title": "Out of range date",
                "type": "decision",
                "tags": "[api, cache]",
                "certainty": 4,
                "date": "2026-05-01T09:30",
                "project": "/repo/api",
                "branch": "main",
                "session_id": "sess-1",
            },
        )
        _write_note(
            tmp_vault,
            "missing-tag",
            {
                "title": "Missing tag",
                "type": "decision",
                "tags": "[other]",
                "certainty": 4,
                "date": "2026-06-10T09:30",
                "project": "/repo/api",
                "branch": "main",
                "session_id": "sess-1",
            },
        )

        filters = dict(
            project="/repo/api",
            note_type="decision",
            tag="cache",
            certainty_min=3,
            date_start="2026-06-01",
            date_end="2026-06-30",
            branch="main",
            session_id="sess-1",
        )

        expected_paths = {entry["path"] for entry in query_notes(tmp_vault, **filters)["results"]}
        assert expected_paths == {"notes/match-all.md"}

        predicate, _ = build_metadata_filter(**filters)
        records = _iter_note_records(tmp_vault)
        actual_paths = {record["path"] for record in records if predicate(record)}

        assert actual_paths == expected_paths
