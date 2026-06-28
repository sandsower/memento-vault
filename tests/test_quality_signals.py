"""Tests for retrieval quality signals and positive project matching (MEM-65)."""

from unittest.mock import patch

import pytest

from memento.graph import read_note_metadata
from memento.search import apply_quality_signals, filter_by_project


def _write_note(vault, stem, frontmatter_lines, body="Body text."):
    notes = vault / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    content = "---\n" + "\n".join(frontmatter_lines) + "\n---\n\n" + body + "\n"
    (notes / f"{stem}.md").write_text(content)
    return f"notes/{stem}.md"


@pytest.fixture
def vault(tmp_path):
    return tmp_path / "vault"


def _result(path, score=0.9):
    return {"path": path, "title": path, "score": score, "snippet": ""}


class TestReadNoteMetadataTags:
    def test_parses_inline_tag_list(self, vault):
        _write_note(vault, "tagged", ["title: Tagged", "type: session", "tags: [pi, queued]"])

        with patch("memento.graph.get_vault", return_value=vault):
            meta = read_note_metadata("tagged")

        assert meta["tags"] == ["pi", "queued"]
        assert meta["type"] == "session"

    def test_missing_tags_returns_empty_list(self, vault):
        _write_note(vault, "untagged", ["title: Untagged", "type: discovery"])

        with patch("memento.graph.get_vault", return_value=vault):
            meta = read_note_metadata("untagged")

        assert meta["tags"] == []


class TestApplyQualitySignals:
    def test_drops_queued_pi_session_captures(self, vault):
        path = _write_note(
            vault,
            "pi-session-candidate-capture-3",
            ["title: Pi session candidate capture", "type: session", "tags: [pi, queued]"],
        )
        results = [_result(path), _result("notes/real-note.md", 0.5)]
        _write_note(vault, "real-note", ["title: Real", "type: decision", "tags: [caching]", "certainty: 4"])

        logged = []
        with (
            patch("memento.graph.get_vault", return_value=vault),
            patch("memento.search.log_retrieval", side_effect=lambda *a, **k: logged.append((a, k))),
        ):
            kept = apply_quality_signals(results, config={"quality_signals_enabled": True})

        assert [r["path"] for r in kept] == ["notes/real-note.md"]
        assert any(k.get("reason") == "queued-session-capture" for _, k in logged)

    def test_drops_log_shaped_paths(self, vault):
        results = [
            _result("fleeting/2026-06-10.md", 0.95),
            _result("projects/memento-vault.md", 0.9),
            _result("notes/valid.md", 0.5),
        ]
        _write_note(vault, "valid", ["title: Valid", "type: decision", "certainty: 4"])

        logged = []
        with (
            patch("memento.graph.get_vault", return_value=vault),
            patch("memento.search.log_retrieval", side_effect=lambda *a, **k: logged.append((a, k))),
        ):
            kept = apply_quality_signals(results, config={"quality_signals_enabled": True})

        assert [r["path"] for r in kept] == ["notes/valid.md"]
        log_shaped = [k["path"] for _, k in logged if k.get("reason") == "log-shaped"]
        assert log_shaped == ["fleeting/2026-06-10.md", "projects/memento-vault.md"]

    def test_penalizes_plain_session_notes(self, vault):
        path = _write_note(vault, "plain-session-note", ["title: Manual note", "type: session", "tags: [memento]"])
        results = [_result(path, 0.8)]

        with patch("memento.graph.get_vault", return_value=vault):
            kept = apply_quality_signals(results, config={"quality_signals_enabled": True})

        assert len(kept) == 1
        assert kept[0]["score"] == pytest.approx(0.8 * 0.85)

    def test_legacy_pi_session_notes_are_handled_as_low_certainty_discoveries(self, vault):
        path = _write_note(vault, "pi-manual-note", ["title: Manual Pi note", "type: session", "tags: [pi]"])
        results = [_result(path, 0.8)]

        with patch("memento.graph.get_vault", return_value=vault):
            kept = apply_quality_signals(results, config={"quality_signals_enabled": True})

        assert len(kept) == 1
        assert kept[0]["score"] == pytest.approx(0.8 * 0.9)

    def test_penalizes_low_certainty(self, vault):
        path = _write_note(vault, "uncertain", ["title: Uncertain", "type: discovery", "certainty: 1"])
        results = [_result(path, 1.0)]

        with patch("memento.graph.get_vault", return_value=vault):
            kept = apply_quality_signals(results, config={"quality_signals_enabled": True})

        assert kept[0]["score"] == pytest.approx(0.9)

    def test_valid_typed_note_score_unchanged(self, vault):
        path = _write_note(vault, "valid", ["title: Valid", "type: decision", "tags: [caching]", "certainty: 4"])
        results = [_result(path, 0.7)]

        with patch("memento.graph.get_vault", return_value=vault):
            kept = apply_quality_signals(results, config={"quality_signals_enabled": True})

        assert kept[0]["score"] == 0.7

    def test_reorders_after_penalties(self, vault):
        session = _write_note(vault, "session-note", ["title: S", "type: session"])
        decision = _write_note(vault, "decision-note", ["title: D", "type: decision", "certainty: 4"])
        results = [_result(session, 0.8), _result(decision, 0.75)]

        with patch("memento.graph.get_vault", return_value=vault):
            kept = apply_quality_signals(results, config={"quality_signals_enabled": True})

        assert [r["path"] for r in kept] == [decision, session]

    def test_disabled_passthrough(self, vault):
        results = [_result("fleeting/2026-06-10.md", 0.95)]

        kept = apply_quality_signals(results, config={"quality_signals_enabled": False})

        assert kept == results

    def test_string_factor_config_survives_simple_yaml(self, vault):
        # the fallback YAML parser returns float overrides as strings
        path = _write_note(vault, "session-y", ["title: S", "type: session"])
        results = [_result(path, 1.0)]

        with patch("memento.graph.get_vault", return_value=vault):
            kept = apply_quality_signals(
                results,
                config={"quality_signals_enabled": True, "quality_session_note_factor": "0.5"},
            )

        assert kept[0]["score"] == pytest.approx(0.5)


class TestRequireProjectMatch:
    def test_default_keeps_notes_without_project(self, vault):
        path = _write_note(vault, "general", ["title: General", "type: discovery"])
        results = [_result(path)]

        with patch("memento.graph.get_vault", return_value=vault):
            kept = filter_by_project(results, "/some/project")

        assert len(kept) == 1

    def test_require_match_drops_notes_without_project(self, vault, tmp_path):
        no_project = _write_note(vault, "general", ["title: General", "type: discovery"])
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        matching = _write_note(vault, "scoped", ["title: Scoped", "type: decision", f"project: {project_dir}"])
        results = [_result(no_project), _result(matching)]

        logged = []
        with (
            patch("memento.graph.get_vault", return_value=vault),
            patch("memento.search.log_retrieval", side_effect=lambda *a, **k: logged.append((a, k))),
        ):
            kept = filter_by_project(results, str(project_dir), require_match=True)

        assert [r["path"] for r in kept] == [matching]
        assert any(k.get("reason") == "no-project-field" for _, k in logged)

    def test_require_match_drops_unreadable_metadata(self, vault, tmp_path):
        results = [_result("notes/missing-note.md")]
        (vault / "notes").mkdir(parents=True, exist_ok=True)

        logged = []
        with (
            patch("memento.graph.get_vault", return_value=vault),
            patch("memento.search.log_retrieval", side_effect=lambda *a, **k: logged.append((a, k))),
        ):
            kept = filter_by_project(results, str(tmp_path), require_match=True)

        assert kept == []
        assert any(k.get("reason") == "no-metadata" for _, k in logged)


class TestToolContextRequiresProjectMatch:
    def test_build_tool_context_passes_require_project_match(self, tmp_path):
        from memento.lifecycle import build_tool_context

        captured = {}

        def _fake_enhance(results, config=None, cwd=None, require_project_match=False):
            captured["require_project_match"] = require_project_match
            return []

        with (
            patch("memento.lifecycle.has_qmd", return_value=True),
            patch("memento.lifecycle.load_cache", return_value={"dirs": {}, "last_qmd_call": 0, "injections": {}}),
            patch("memento.lifecycle.save_cache"),
            patch("memento.lifecycle.qmd_search_with_extras", return_value=[{"path": "notes/x.md", "score": 0.9}]),
            patch("memento.lifecycle.enhance_results", side_effect=_fake_enhance),
        ):
            build_tool_context("Read", "/workspace/src/server/authMiddleware.ts", "/repo", "s1")

        assert captured["require_project_match"] is True


class TestQualitySignalNormalization:
    def test_quoted_and_cased_frontmatter_still_dropped(self, vault):
        path = _write_note(
            vault,
            "cased-capture",
            ["title: Cased capture", 'type: "Session"', "tags: [PI, QUEUED]"],
        )
        results = [_result(path)]

        with patch("memento.graph.get_vault", return_value=vault):
            kept = apply_quality_signals(results, config={"quality_signals_enabled": True})

        assert kept == []

    def test_cased_session_type_still_penalized(self, vault):
        path = _write_note(vault, "cased-session", ["title: S", "type: Session"])
        results = [_result(path, 1.0)]

        with patch("memento.graph.get_vault", return_value=vault):
            kept = apply_quality_signals(results, config={"quality_signals_enabled": True})

        assert kept[0]["score"] == pytest.approx(0.85)
