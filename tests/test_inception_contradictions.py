"""Tests for MEM-163's Inception contradiction detection stage.

Covers the candidate pass (embedding-similarity pairs, project scoping,
non-invalidated requirement), the strict-JSON verdict parser, date
ordering, and the adjudication orchestration (auto-apply, review-queue,
malformed-verdict resilience) via a scripted fake ``llm_complete``/``call_llm``.
"""

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from memento_inception import (
    NoteRecord,
    _order_by_date,
    find_contradiction_candidates,
    main,
    parse_args,
    parse_contradiction_verdict,
    run_contradiction_detection,
)


def _note(
    stem,
    *,
    date="2026-01-01T10:00",
    project="proj-a",
    invalidated_by=None,
    supersedes=None,
    body="Body text.",
    title=None,
):
    return NoteRecord(
        stem=stem,
        path=Path(f"/vault/notes/{stem}.md"),
        title=title or stem,
        note_type="discovery",
        tags=[],
        date=date,
        project=project,
        invalidated_by=invalidated_by,
        supersedes=supersedes,
        body=body,
    )


class TestFindContradictionCandidates:
    def test_pairs_above_threshold_same_project(self):
        notes = {"a": _note("a"), "b": _note("b")}
        stem_index = ["a", "b"]
        # Identical vectors -> cosine similarity 1.0
        matrix = np.array([[1.0, 0.0], [1.0, 0.0]])

        candidates = find_contradiction_candidates(
            stem_index, matrix, notes, {"contradiction_similarity_threshold": 0.5}
        )

        assert candidates == [("a", "b", pytest.approx(1.0))]

    def test_below_threshold_pairs_are_excluded(self):
        notes = {"a": _note("a"), "b": _note("b")}
        stem_index = ["a", "b"]
        matrix = np.array([[1.0, 0.0], [0.0, 1.0]])  # orthogonal -> similarity 0.0

        candidates = find_contradiction_candidates(
            stem_index, matrix, notes, {"contradiction_similarity_threshold": 0.5}
        )

        assert candidates == []

    def test_different_projects_are_excluded(self):
        notes = {"a": _note("a", project="proj-a"), "b": _note("b", project="proj-b")}
        stem_index = ["a", "b"]
        matrix = np.array([[1.0, 0.0], [1.0, 0.0]])

        candidates = find_contradiction_candidates(
            stem_index, matrix, notes, {"contradiction_similarity_threshold": 0.5}
        )

        assert candidates == []

    def test_already_invalidated_notes_are_excluded(self):
        notes = {"a": _note("a", invalidated_by="c"), "b": _note("b")}
        stem_index = ["a", "b"]
        matrix = np.array([[1.0, 0.0], [1.0, 0.0]])

        candidates = find_contradiction_candidates(
            stem_index, matrix, notes, {"contradiction_similarity_threshold": 0.5}
        )

        assert candidates == []

    def test_existing_supersedes_edge_is_excluded(self):
        notes = {"a": _note("a"), "b": _note("b", supersedes="a")}
        stem_index = ["a", "b"]
        matrix = np.array([[1.0, 0.0], [1.0, 0.0]])

        candidates = find_contradiction_candidates(
            stem_index, matrix, notes, {"contradiction_similarity_threshold": 0.5}
        )

        assert candidates == []

    def test_sorted_by_similarity_descending(self):
        notes = {"a": _note("a"), "b": _note("b"), "c": _note("c")}
        stem_index = ["a", "b", "c"]
        # a-b: similarity 1.0; a-c: similarity ~0.6
        matrix = np.array([[1.0, 0.0], [1.0, 0.0], [0.6, 0.8]])

        candidates = find_contradiction_candidates(
            stem_index, matrix, notes, {"contradiction_similarity_threshold": 0.1}
        )

        assert [c[:2] for c in candidates][0] == ("a", "b")

    def test_fewer_than_two_notes_returns_empty(self):
        assert find_contradiction_candidates(["a"], np.array([[1.0, 0.0]]), {"a": _note("a")}, {}) == []


class TestOrderByDate:
    def test_orders_older_first(self):
        older = _note("old", date="2026-01-01T10:00")
        newer = _note("new", date="2026-02-01T10:00")
        result_older, result_newer = _order_by_date(newer, older)
        assert result_older.stem == "old"
        assert result_newer.stem == "new"

    def test_tied_dates_return_none(self):
        a = _note("a", date="2026-01-01T10:00")
        b = _note("b", date="2026-01-01T10:00")
        assert _order_by_date(a, b) == (None, None)

    def test_unparseable_dates_return_none(self):
        a = _note("a", date="not-a-date")
        b = _note("b", date="2026-01-01T10:00")
        assert _order_by_date(a, b) == (None, None)

    def test_missing_date_returns_none(self):
        a = _note("a", date="")
        b = _note("b", date="2026-01-01T10:00")
        assert _order_by_date(a, b) == (None, None)


class TestParseContradictionVerdict:
    def test_valid_verdict(self):
        raw = json.dumps({"contradicts": True, "newer_wins": True, "confidence": 0.9})
        assert parse_contradiction_verdict(raw) == {"contradicts": True, "newer_wins": True, "confidence": 0.9}

    def test_code_fenced_verdict(self):
        inner = json.dumps({"contradicts": False, "newer_wins": False, "confidence": 0.2})
        raw = f"```json\n{inner}\n```"
        assert parse_contradiction_verdict(raw) == {"contradicts": False, "newer_wins": False, "confidence": 0.2}

    def test_malformed_json_returns_none(self):
        assert parse_contradiction_verdict("not json at all") is None

    def test_empty_returns_none(self):
        assert parse_contradiction_verdict("") is None

    def test_non_bool_contradicts_returns_none(self):
        raw = json.dumps({"contradicts": "yes", "newer_wins": True, "confidence": 0.9})
        assert parse_contradiction_verdict(raw) is None

    def test_missing_keys_returns_none(self):
        raw = json.dumps({"contradicts": True})
        assert parse_contradiction_verdict(raw) is None

    def test_out_of_range_confidence_returns_none(self):
        raw = json.dumps({"contradicts": True, "newer_wins": True, "confidence": 1.5})
        assert parse_contradiction_verdict(raw) is None

    def test_unparseable_confidence_returns_none(self):
        raw = json.dumps({"contradicts": True, "newer_wins": True, "confidence": "high"})
        assert parse_contradiction_verdict(raw) is None


class TestRunContradictionDetection:
    def _setup(self, tmp_path, *, older_date="2026-01-01T10:00", newer_date="2026-02-01T10:00"):
        vault = tmp_path / "vault"
        notes_dir = vault / "notes"
        notes_dir.mkdir(parents=True)
        old_path = notes_dir / "old-note.md"
        new_path = notes_dir / "new-note.md"
        old_path.write_text(f"---\ntitle: Old note\ntype: discovery\ndate: {older_date}\n---\n\nOld body.\n")
        new_path.write_text(f"---\ntitle: New note\ntype: discovery\ndate: {newer_date}\n---\n\nNew body.\n")
        notes_dict = {
            "old-note": _note("old-note", date=older_date, body="Old body.", title="Old note"),
            "new-note": _note("new-note", date=newer_date, body="New body.", title="New note"),
        }
        notes_dict["old-note"].path = old_path
        notes_dict["new-note"].path = new_path
        stem_index = ["old-note", "new-note"]
        matrix = np.array([[1.0, 0.0], [1.0, 0.0]])
        return vault, notes_dict, stem_index, matrix

    def test_auto_applies_when_contradicts_and_newer_wins_and_confident(self, tmp_path):
        vault, notes_dict, stem_index, matrix = self._setup(tmp_path)
        queue_path = tmp_path / "review-queue.jsonl"
        verdict = json.dumps({"contradicts": True, "newer_wins": True, "confidence": 0.9})

        with patch("memento_inception.call_llm", return_value=verdict):
            report = run_contradiction_detection(
                stem_index,
                matrix,
                notes_dict,
                {"contradiction_similarity_threshold": 0.5, "contradiction_confidence_threshold": 0.75},
                vault,
                review_queue_path=str(queue_path),
            )

        assert report["candidates_found"] == 1
        assert report["pairs_adjudicated"] == 1
        assert report["auto_applied"] == [
            {"older": "old-note", "newer": "new-note", "confidence": 0.9, "applied": True}
        ]
        assert report["queued_for_review"] == []
        text = (vault / "notes" / "old-note.md").read_text()
        assert "invalidated_by: new-note" in text
        assert not queue_path.exists()

    def test_below_confidence_threshold_queues_for_review(self, tmp_path):
        vault, notes_dict, stem_index, matrix = self._setup(tmp_path)
        queue_path = tmp_path / "review-queue.jsonl"
        verdict = json.dumps({"contradicts": True, "newer_wins": True, "confidence": 0.5})

        with patch("memento_inception.call_llm", return_value=verdict):
            report = run_contradiction_detection(
                stem_index,
                matrix,
                notes_dict,
                {"contradiction_similarity_threshold": 0.5, "contradiction_confidence_threshold": 0.75},
                vault,
                review_queue_path=str(queue_path),
            )

        assert report["auto_applied"] == []
        assert len(report["queued_for_review"]) == 1
        assert report["queued_for_review"][0]["reason"] == "below-policy-threshold"
        text = (vault / "notes" / "old-note.md").read_text()
        assert "invalidated_by" not in text
        assert queue_path.exists()
        queued = json.loads(queue_path.read_text().splitlines()[0])
        assert queued["stem_a"] == "old-note"
        assert queued["stem_b"] == "new-note"

    def test_newer_wins_false_queues_for_review(self, tmp_path):
        vault, notes_dict, stem_index, matrix = self._setup(tmp_path)
        queue_path = tmp_path / "review-queue.jsonl"
        verdict = json.dumps({"contradicts": True, "newer_wins": False, "confidence": 0.95})

        with patch("memento_inception.call_llm", return_value=verdict):
            report = run_contradiction_detection(
                stem_index,
                matrix,
                notes_dict,
                {"contradiction_similarity_threshold": 0.5, "contradiction_confidence_threshold": 0.75},
                vault,
                review_queue_path=str(queue_path),
            )

        assert report["auto_applied"] == []
        assert len(report["queued_for_review"]) == 1

    def test_malformed_verdict_is_resilient_and_queues_for_review(self, tmp_path):
        vault, notes_dict, stem_index, matrix = self._setup(tmp_path)
        queue_path = tmp_path / "review-queue.jsonl"

        with patch("memento_inception.call_llm", return_value="not valid json"):
            report = run_contradiction_detection(
                stem_index,
                matrix,
                notes_dict,
                {"contradiction_similarity_threshold": 0.5},
                vault,
                review_queue_path=str(queue_path),
            )

        assert report["malformed"] == 1
        assert report["auto_applied"] == []
        assert len(report["queued_for_review"]) == 1
        assert report["queued_for_review"][0]["reason"] == "malformed-verdict"
        queued = json.loads(queue_path.read_text().splitlines()[0])
        assert queued["reason"] == "malformed-verdict"

    def test_unparseable_dates_skip_llm_call_and_queue_directly(self, tmp_path):
        vault, notes_dict, stem_index, matrix = self._setup(tmp_path, older_date="", newer_date="")
        queue_path = tmp_path / "review-queue.jsonl"

        with patch("memento_inception.call_llm") as mock_llm:
            report = run_contradiction_detection(
                stem_index,
                matrix,
                notes_dict,
                {"contradiction_similarity_threshold": 0.5},
                vault,
                review_queue_path=str(queue_path),
            )

        mock_llm.assert_not_called()
        assert report["pairs_adjudicated"] == 0
        assert len(report["queued_for_review"]) == 1
        assert report["queued_for_review"][0]["reason"] == "unparseable-or-tied-dates"

    def test_bounded_by_max_pairs_per_run(self, tmp_path):
        vault = tmp_path / "vault"
        notes_dir = vault / "notes"
        notes_dir.mkdir(parents=True)
        notes_dict = {}
        stem_index = []
        vectors = []
        for i in range(5):
            stem = f"note-{i}"
            path = notes_dir / f"{stem}.md"
            path.write_text(f"---\ntitle: Note {i}\ntype: discovery\ndate: 2026-01-0{i + 1}T10:00\n---\n\nBody.\n")
            rec = _note(stem, date=f"2026-01-0{i + 1}T10:00")
            rec.path = path
            notes_dict[stem] = rec
            stem_index.append(stem)
            vectors.append([1.0, 0.0])  # all identical -> all pairs above threshold

        matrix = np.array(vectors)
        queue_path = tmp_path / "review-queue.jsonl"
        verdict = json.dumps({"contradicts": False, "newer_wins": False, "confidence": 0.1})

        with patch("memento_inception.call_llm", return_value=verdict) as mock_llm:
            report = run_contradiction_detection(
                stem_index,
                matrix,
                notes_dict,
                {"contradiction_similarity_threshold": 0.5, "contradiction_max_pairs_per_run": 2},
                vault,
                review_queue_path=str(queue_path),
            )

        # 5 notes -> 10 possible pairs, but only 2 are adjudicated (bounded).
        assert report["candidates_found"] == 10
        assert report["pairs_adjudicated"] == 2
        assert mock_llm.call_count == 2

    def test_no_candidates_returns_empty_report(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        report = run_contradiction_detection(["a"], np.array([[1.0, 0.0]]), {"a": _note("a")}, {}, vault)
        assert report == {
            "candidates_found": 0,
            "pairs_adjudicated": 0,
            "auto_applied": [],
            "queued_for_review": [],
            "malformed": 0,
        }


class TestMainInvokesContradictionDetection:
    """MEM-163 stage in main() is gated and failure-isolated like the sweeper's stages."""

    def _run_main(self, config, state_path, argv, **kwargs):
        args = parse_args(argv)
        with patch("memento_inception.get_config", return_value=config):
            return main(args, state_path=str(state_path), **kwargs)

    def test_disabled_by_default_skips_contradiction_detection(
        self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db
    ):
        with patch("memento_inception.run_contradiction_detection") as mock_run:
            result = self._run_main(mock_config, inception_state_path, ["--full"], db_path=str(mock_qmd_db))
        assert result == 0
        mock_run.assert_not_called()

    def test_enabled_runs_contradiction_detection_and_is_failure_isolated(
        self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db
    ):
        mock_config["contradiction_detection_enabled"] = True

        def _boom(*args, **kwargs):
            raise RuntimeError("contradiction detection exploded")

        with patch("memento_inception.run_contradiction_detection", side_effect=_boom) as mock_run:
            with patch("memento_inception.call_llm", return_value=""):
                result = self._run_main(mock_config, inception_state_path, ["--full"], db_path=str(mock_qmd_db))

        assert result == 0  # failure isolated -- clustering/synthesis still ran to completion
        mock_run.assert_called_once()
