"""shape_search_results tags each entry with its origin (profile vs note)."""

from __future__ import annotations

from memento.search import shape_search_results


def test_shape_search_results_tags_source(tmp_path):
    results = [
        {"path": "profile/voice.md", "title": "voice", "score": 0.9, "backend": "embedded-fts"},
        {"path": "notes/foo.md", "title": "foo", "score": 0.5, "backend": "embedded-fts"},
        {"path": "projects/bar.md", "title": "bar", "score": 0.4, "backend": "qmd"},
    ]

    shaped = shape_search_results(results, vault=tmp_path)
    by_path = {entry["path"]: entry for entry in shaped["results"]}

    assert by_path["profile/voice.md"]["source"] == "profile"
    assert by_path["notes/foo.md"]["source"] == "note"
    assert by_path["projects/bar.md"]["source"] == "note"
    # source sits alongside the existing backend field
    assert by_path["profile/voice.md"]["backend"] == "embedded-fts"
