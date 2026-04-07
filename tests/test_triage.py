"""Tests for memento-triage: parse_transcript, is_substantial, write_fleeting."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Import memento-triage.py (hyphenated filename)
_spec = importlib.util.spec_from_file_location(
    "memento_triage",
    str(Path(__file__).parent.parent / "hooks" / "memento-triage.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["memento_triage"] = _mod
_spec.loader.exec_module(_mod)

parse_transcript = _mod.parse_transcript
is_substantial = _mod.is_substantial
write_fleeting = _mod.write_fleeting


def _write_transcript(tmp_path, entries):
    """Write a JSONL transcript file from a list of dicts."""
    path = tmp_path / "transcript.jsonl"
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return str(path)


def _user_msg(text, cwd="/home/user/project", branch="main"):
    return {
        "type": "user",
        "cwd": cwd,
        "gitBranch": branch,
        "message": {"content": text},
    }


def _assistant_msg(blocks):
    return {
        "type": "assistant",
        "message": {"content": blocks},
    }


def _text_block(text):
    return {"type": "text", "text": text}


def _edit_block(path):
    return {"type": "tool_use", "name": "Edit", "input": {"file_path": path}}


def _read_block(path):
    return {"type": "tool_use", "name": "Read", "input": {"file_path": path}}


# --- parse_transcript ---


class TestParseTranscript:
    def test_basic_parsing(self, tmp_path):
        entries = [
            _user_msg("How do I fix this bug?"),
            _assistant_msg([_text_block("Here's the fix."), _edit_block("/src/main.py")]),
        ]
        meta = parse_transcript(_write_transcript(tmp_path, entries))
        assert meta["exchange_count"] == 1
        assert meta["files_edited"] == ["/src/main.py"]
        assert meta["cwd"] == "/home/user/project"
        assert meta["git_branch"] == "main"
        assert "fix this bug" in meta["first_prompt"]

    def test_tracks_files_read(self, tmp_path):
        entries = [
            _user_msg("Read these files"),
            _assistant_msg([
                _read_block("/src/a.py"),
                _read_block("/src/b.py"),
                _read_block("/src/c.py"),
                _read_block("/src/d.py"),
                _read_block("/src/e.py"),
                _read_block("/src/f.py"),
            ]),
        ]
        meta = parse_transcript(_write_transcript(tmp_path, entries))
        assert len(meta["files_read"]) == 6

    def test_captures_last_outcome(self, tmp_path):
        entries = [
            _user_msg("What's wrong?"),
            _assistant_msg([_text_block("Looking into it...")]),
            _user_msg("Any progress?"),
            _assistant_msg([_text_block("Fixed the null pointer. The issue was an uninitialized variable in the loop.")]),
        ]
        meta = parse_transcript(_write_transcript(tmp_path, entries))
        assert meta["last_outcome"] is not None
        assert "Fixed the null pointer" in meta["last_outcome"]

    def test_last_outcome_truncated(self, tmp_path):
        long_text = "A" * 200
        entries = [
            _user_msg("Help"),
            _assistant_msg([_text_block(long_text)]),
        ]
        meta = parse_transcript(_write_transcript(tmp_path, entries))
        assert len(meta["last_outcome"]) <= 104  # 100 + "..."

    def test_last_outcome_first_sentence(self, tmp_path):
        entries = [
            _user_msg("Help"),
            _assistant_msg([_text_block("The fix was simple. We just needed to add a null check.")]),
        ]
        meta = parse_transcript(_write_transcript(tmp_path, entries))
        assert meta["last_outcome"] == "The fix was simple."


# --- is_substantial ---


_DEFAULT_CONFIG = {
    "exchange_threshold": 15,
    "file_count_threshold": 3,
    "notable_patterns": ["MEMORY.md", "CLAUDE.md"],
}


class TestIsSubstantial:
    def _with_config(self, meta):
        with patch("memento_triage.get_config", return_value=_DEFAULT_CONFIG):
            return is_substantial(meta)

    def test_high_exchange_count(self):
        meta = {"exchange_count": 20, "files_edited": [], "first_prompt": None}
        assert self._with_config(meta) is True

    def test_many_files_edited(self):
        meta = {"exchange_count": 2, "files_edited": ["a", "b", "c", "d"], "first_prompt": None}
        assert self._with_config(meta) is True

    def test_notable_pattern(self):
        meta = {"exchange_count": 2, "files_edited": ["/proj/CLAUDE.md"], "first_prompt": None}
        assert self._with_config(meta) is True

    def test_trivial_session_rejected(self):
        meta = {"exchange_count": 3, "files_edited": [], "first_prompt": "hello"}
        assert self._with_config(meta) is False

    def test_keyword_match_with_5_exchanges(self):
        meta = {
            "exchange_count": 5,
            "files_edited": [],
            "first_prompt": "How to fix the crash in production?",
        }
        assert self._with_config(meta) is True

    def test_keyword_match_under_5_exchanges(self):
        meta = {
            "exchange_count": 3,
            "files_edited": [],
            "first_prompt": "fix the bug",
        }
        assert self._with_config(meta) is False

    def test_keyword_no_match(self):
        meta = {
            "exchange_count": 7,
            "files_edited": [],
            "first_prompt": "add a button to the navbar",
        }
        assert self._with_config(meta) is False

    def test_read_heavy_session(self):
        meta = {
            "exchange_count": 2,
            "files_edited": [],
            "files_read": [f"/src/{i}.py" for i in range(6)],
            "first_prompt": "look at this code",
        }
        assert self._with_config(meta) is True

    def test_read_heavy_under_threshold(self):
        meta = {
            "exchange_count": 2,
            "files_edited": [],
            "files_read": [f"/src/{i}.py" for i in range(5)],
            "first_prompt": "look at this code",
        }
        assert self._with_config(meta) is False


# --- write_fleeting ---


class TestWriteFleeting:
    def test_includes_outcome(self, tmp_vault):
        with patch("memento_triage.get_vault", return_value=tmp_vault):
            meta = {
                "cwd": "/home/user/project",
                "git_branch": "main",
                "exchange_count": 5,
                "files_edited": [],
                "first_prompt": "What's wrong?",
                "last_outcome": "Fixed the null pointer.",
            }
            write_fleeting("abc12345", meta, "my-project")

        fleeting_files = list((tmp_vault / "fleeting").glob("*.md"))
        assert len(fleeting_files) == 1
        content = fleeting_files[0].read_text()
        assert "→ Fixed the null pointer." in content

    def test_no_outcome(self, tmp_vault):
        with patch("memento_triage.get_vault", return_value=tmp_vault):
            meta = {
                "cwd": "/home/user/project",
                "git_branch": "main",
                "exchange_count": 3,
                "files_edited": [],
                "first_prompt": "hello",
                "last_outcome": None,
            }
            write_fleeting("abc12345", meta, "my-project")

        fleeting_files = list((tmp_vault / "fleeting").glob("*.md"))
        content = fleeting_files[0].read_text()
        assert "→" not in content
