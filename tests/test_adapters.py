"""Tests for transcript parsing adapters."""

import json
import os
from unittest.mock import patch

import pytest

from memento.adapters import detect_agent, parse_transcript
from memento.adapters.claude import parse_transcript as parse_claude


@pytest.fixture
def claude_transcript(tmp_path):
    """Create a minimal Claude Code JSONL transcript."""
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        json.dumps(
            {
                "type": "user",
                "cwd": "/home/vic/Projects/test",
                "gitBranch": "main",
                "message": {"content": "Fix the broken login flow"},
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "I'll fix the login flow."},
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "/home/vic/Projects/test/auth.py"},
                        },
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {"content": "Looks good, ship it"},
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Done. The login flow is fixed."},
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": "/home/vic/Projects/test/auth.py"},
                        },
                    ]
                },
            }
        ),
    ]
    transcript.write_text("\n".join(lines))
    return transcript


@pytest.fixture
def unknown_transcript(tmp_path):
    """Create a transcript in an unrecognized format."""
    transcript = tmp_path / "unknown.jsonl"
    transcript.write_text(json.dumps({"format": "something_else", "data": []}) + "\n")
    return transcript


# --- detect_agent ---


class TestDetectAgent:
    def test_env_var_override(self, claude_transcript):
        with patch.dict(os.environ, {"MEMENTO_AGENT": "codex"}):
            assert detect_agent(str(claude_transcript)) == "codex"

    def test_env_var_case_insensitive(self, claude_transcript):
        with patch.dict(os.environ, {"MEMENTO_AGENT": "CURSOR"}):
            assert detect_agent(str(claude_transcript)) == "cursor"

    def test_detects_claude_from_transcript(self, claude_transcript):
        assert detect_agent(str(claude_transcript)) == "claude"

    def test_unknown_format(self, unknown_transcript):
        assert detect_agent(str(unknown_transcript)) == "unknown"

    def test_nonexistent_file(self, tmp_path):
        assert detect_agent(str(tmp_path / "nope.jsonl")) == "unknown"

    def test_empty_file(self, tmp_path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        assert detect_agent(str(empty)) == "unknown"

    def test_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.jsonl"
        bad.write_text("not json at all\n")
        assert detect_agent(str(bad)) == "unknown"


# --- parse_transcript (dispatcher) ---


class TestParseTranscript:
    def test_claude_auto_detect(self, claude_transcript):
        meta = parse_transcript(str(claude_transcript))
        assert meta["agent"] == "claude"
        assert meta["cwd"] == "/home/vic/Projects/test"
        assert meta["git_branch"] == "main"
        assert meta["exchange_count"] == 2
        assert "/home/vic/Projects/test/auth.py" in meta["files_edited"]
        assert "/home/vic/Projects/test/auth.py" in meta["files_read"]

    def test_explicit_agent_override_raises_for_unimplemented(self, claude_transcript):
        with pytest.raises(ValueError, match="not yet implemented"):
            parse_transcript(str(claude_transcript), agent="codex")

    def test_unknown_agent_raises(self, unknown_transcript):
        with pytest.raises(ValueError, match="Unknown agent"):
            parse_transcript(str(unknown_transcript))

    def test_env_var_agent_raises_for_unimplemented(self, claude_transcript):
        with patch.dict(os.environ, {"MEMENTO_AGENT": "windsurf"}):
            with pytest.raises(ValueError, match="not yet implemented"):
                parse_transcript(str(claude_transcript))


# --- Claude adapter ---


class TestClaudeAdapter:
    def test_parses_metadata(self, claude_transcript):
        meta = parse_claude(str(claude_transcript))
        assert meta["cwd"] == "/home/vic/Projects/test"
        assert meta["git_branch"] == "main"
        assert meta["exchange_count"] == 2
        assert meta["user_messages"] == 2

    def test_extracts_files(self, claude_transcript):
        meta = parse_claude(str(claude_transcript))
        assert meta["files_edited"] == ["/home/vic/Projects/test/auth.py"]
        assert meta["files_read"] == ["/home/vic/Projects/test/auth.py"]

    def test_extracts_first_prompt(self, claude_transcript):
        meta = parse_claude(str(claude_transcript))
        assert meta["first_prompt"] == "Fix the broken login flow"

    def test_extracts_last_outcome(self, claude_transcript):
        meta = parse_claude(str(claude_transcript))
        # last_outcome is first sentence of last assistant text
        assert meta["last_outcome"] == "Done."

    def test_strips_system_tags_from_prompt(self, tmp_path):
        transcript = tmp_path / "tagged.jsonl"
        lines = [
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": "<system-reminder>ignore</system-reminder>Do the thing"},
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Done."}]},
                }
            ),
        ]
        transcript.write_text("\n".join(lines))
        meta = parse_claude(str(transcript))
        assert meta["first_prompt"] == "Do the thing"

    def test_empty_transcript(self, tmp_path):
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")
        meta = parse_claude(str(transcript))
        assert meta["exchange_count"] == 0
        assert meta["files_edited"] == []
