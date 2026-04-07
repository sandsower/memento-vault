"""Tests for Inception state management functions in memento.store."""

import json
import os


from memento.store import load_inception_state, save_inception_state


class TestLoadInceptionState:
    def test_load_state_missing_file(self, tmp_path):
        """load from nonexistent path returns defaults."""
        path = str(tmp_path / "does-not-exist" / "inception-state.json")
        state = load_inception_state(state_path=path)
        assert state == {
            "last_run_iso": None,
            "last_run_note_count": 0,
            "runs": [],
            "processed_notes": [],
        }

    def test_load_state_valid(self, inception_state_path):
        """write valid JSON, load returns it with defaults filled."""
        partial = {"last_run_iso": "2026-03-22T10:00:00", "custom_key": "keep"}
        inception_state_path.write_text(json.dumps(partial))

        state = load_inception_state(state_path=str(inception_state_path))

        assert state["last_run_iso"] == "2026-03-22T10:00:00"
        assert state["custom_key"] == "keep"
        # defaults filled in
        assert state["last_run_note_count"] == 0
        assert state["runs"] == []
        assert state["processed_notes"] == []

    def test_load_state_corrupt(self, inception_state_path):
        """write invalid JSON, load returns defaults and creates .bak."""
        inception_state_path.write_text("{invalid json???")

        state = load_inception_state(state_path=str(inception_state_path))

        assert state == {
            "last_run_iso": None,
            "last_run_note_count": 0,
            "runs": [],
            "processed_notes": [],
        }
        bak = str(inception_state_path) + ".bak"
        assert os.path.exists(bak)
        # original file should have been renamed
        assert not inception_state_path.exists()


class TestSaveInceptionState:
    def test_save_state_creates_dir(self, tmp_path):
        """save to non-existent dir, verify file created."""
        path = str(tmp_path / "nested" / "dir" / "inception-state.json")
        state = {"last_run_iso": "2026-03-22T12:00:00", "runs": []}
        save_inception_state(state, state_path=path)

        assert os.path.exists(path)
        loaded = json.loads(open(path).read())
        assert loaded["last_run_iso"] == "2026-03-22T12:00:00"

    def test_save_state_truncates_runs(self, inception_state_path):
        """save with 15 runs, verify only last 10 remain."""
        runs = [{"ts": f"2026-03-{i:02d}"} for i in range(1, 16)]
        state = {"runs": runs}
        save_inception_state(state, state_path=str(inception_state_path))

        loaded = json.loads(inception_state_path.read_text())
        assert len(loaded["runs"]) == 10
        # should keep the last 10 (indices 5..14, i.e. days 06..15)
        assert loaded["runs"][0]["ts"] == "2026-03-06"
        assert loaded["runs"][-1]["ts"] == "2026-03-15"


class TestSaveLoadRoundtrip:
    def test_save_load_roundtrip(self, inception_state_path):
        """save then load, verify data matches."""
        original = {
            "last_run_iso": "2026-03-22T09:30:00",
            "last_run_note_count": 5,
            "runs": [{"ts": "2026-03-22", "notes": 5}],
            "processed_notes": ["note-a", "note-b"],
        }
        save_inception_state(original, state_path=str(inception_state_path))
        loaded = load_inception_state(state_path=str(inception_state_path))

        assert loaded["last_run_iso"] == original["last_run_iso"]
        assert loaded["last_run_note_count"] == original["last_run_note_count"]
        assert loaded["runs"] == original["runs"]
        assert loaded["processed_notes"] == original["processed_notes"]
