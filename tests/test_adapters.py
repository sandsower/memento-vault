"""Tests for transcript parsing adapters."""

import json
import os
import sqlite3
import uuid
from unittest.mock import patch

import pytest

from memento.adapters import detect_agent, parse_transcript
from memento.adapters.claude import parse_transcript as parse_claude
from memento.adapters.opencode import parse_transcript as parse_opencode


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

    def test_detects_claude_past_metadata_prefix(self, tmp_path):
        transcript = tmp_path / "prefixed.jsonl"
        lines = [
            json.dumps({"type": "file-history-snapshot", "messageId": "m1", "snapshot": {}}),
            json.dumps({"type": "attachment", "messageId": "m1"}),
            json.dumps({"type": "user", "cwd": "/tmp", "message": {"content": "hi"}}),
        ]
        transcript.write_text("\n".join(lines))
        assert detect_agent(str(transcript)) == "claude"

    def test_detects_claude_ignores_invalid_lines_in_prefix(self, tmp_path):
        transcript = tmp_path / "mixed.jsonl"
        transcript.write_text(
            "garbage line\n"
            + json.dumps({"type": "system"})
            + "\n"
            + json.dumps({"type": "user", "message": {"content": "yo"}})
            + "\n"
        )
        assert detect_agent(str(transcript)) == "claude"

    def test_metadata_only_returns_unknown(self, tmp_path):
        transcript = tmp_path / "metadata.jsonl"
        transcript.write_text("\n".join(json.dumps({"type": "file-history-snapshot", "i": i}) for i in range(5)) + "\n")
        assert detect_agent(str(transcript)) == "unknown"


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


# --- OpenCode adapter ---


def _opencode_id(prefix):
    """Build an OpenCode-style ulid-ish id for fixtures."""
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _build_opencode_db(path, sessions):
    """Construct a minimal OpenCode SQLite store.

    ``sessions`` is a list of dicts: ``{"id", "directory", "time_created",
    "messages": [{"role", "time_created", "parts": [...]}]}``.
    """
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            directory TEXT NOT NULL,
            title TEXT NOT NULL,
            version TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    for s in sessions:
        cur.execute(
            "INSERT INTO session (id, project_id, directory, title, version, time_created, time_updated) "
            "VALUES (?, 'prj_test', ?, 'fixture', '1.0', ?, ?)",
            (s["id"], s.get("directory", "/tmp/fixture"), s["time_created"], s["time_created"]),
        )
        for m_idx, m in enumerate(s["messages"]):
            mid = _opencode_id("msg")
            cur.execute(
                "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
                (mid, s["id"], m["time_created"], m["time_created"], json.dumps({"role": m["role"]})),
            )
            for p_idx, p in enumerate(m["parts"]):
                cur.execute(
                    "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _opencode_id("prt"),
                        mid,
                        s["id"],
                        m["time_created"] + p_idx,
                        m["time_created"] + p_idx,
                        json.dumps(p),
                    ),
                )
    conn.commit()
    conn.close()


@pytest.fixture
def opencode_db(tmp_path):
    db = tmp_path / "opencode.db"
    _build_opencode_db(
        db,
        [
            {
                "id": "ses_a",
                "directory": "/home/dev/proj",
                "time_created": 1_000,
                "messages": [
                    {
                        "role": "user",
                        "time_created": 1_001,
                        "parts": [{"type": "text", "text": "Fix the broken login flow"}],
                    },
                    {
                        "role": "assistant",
                        "time_created": 1_002,
                        "parts": [
                            {"type": "step-start"},
                            {"type": "text", "text": "Working on it."},
                            {
                                "type": "tool",
                                "tool": "read",
                                "state": {"input": {"file_path": "/home/dev/proj/auth.py"}},
                            },
                            {
                                "type": "tool",
                                "tool": "edit",
                                "state": {"input": {"file_path": "/home/dev/proj/auth.py"}},
                            },
                            {"type": "step-finish"},
                        ],
                    },
                    {
                        "role": "user",
                        "time_created": 1_003,
                        "parts": [{"type": "text", "text": "ship it"}],
                    },
                    {
                        "role": "assistant",
                        "time_created": 1_004,
                        "parts": [{"type": "text", "text": "Done. Login flow is fixed."}],
                    },
                ],
            }
        ],
    )
    return db


class TestDetectAgentOpencode:
    def test_detects_opencode_from_sqlite(self, opencode_db):
        assert detect_agent(str(opencode_db)) == "opencode"

    def test_env_var_override(self, opencode_db):
        with patch.dict(os.environ, {"MEMENTO_AGENT": "opencode"}):
            assert detect_agent("/nonexistent/path") == "opencode"

    def test_unrelated_sqlite_not_opencode(self, tmp_path):
        db = tmp_path / "other.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE widgets (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        assert detect_agent(str(db)) == "unknown"


class TestOpencodeAdapter:
    def test_parses_metadata(self, opencode_db):
        meta = parse_opencode(str(opencode_db))
        assert meta["cwd"] == "/home/dev/proj"
        assert meta["git_branch"] is None
        assert meta["exchange_count"] == 2
        assert meta["user_messages"] == 2

    def test_extracts_files(self, opencode_db):
        meta = parse_opencode(str(opencode_db))
        assert meta["files_read"] == ["/home/dev/proj/auth.py"]
        assert meta["files_edited"] == ["/home/dev/proj/auth.py"]

    def test_first_prompt_and_outcome(self, opencode_db):
        meta = parse_opencode(str(opencode_db))
        assert meta["first_prompt"] == "Fix the broken login flow"
        assert meta["last_outcome"] == "Done."

    def test_strips_system_tags_from_prompt(self, tmp_path):
        db = tmp_path / "opencode.db"
        _build_opencode_db(
            db,
            [
                {
                    "id": "ses_b",
                    "time_created": 1,
                    "messages": [
                        {
                            "role": "user",
                            "time_created": 2,
                            "parts": [{"type": "text", "text": "<system-reminder>x</system-reminder>Do the thing"}],
                        },
                        {
                            "role": "assistant",
                            "time_created": 3,
                            "parts": [{"type": "text", "text": "Done."}],
                        },
                    ],
                }
            ],
        )
        meta = parse_opencode(str(db))
        assert meta["first_prompt"] == "Do the thing"

    def test_session_id_override_via_env(self, tmp_path):
        db = tmp_path / "opencode.db"
        _build_opencode_db(
            db,
            [
                {
                    "id": "ses_old",
                    "time_created": 1,
                    "messages": [
                        {
                            "role": "user",
                            "time_created": 2,
                            "parts": [{"type": "text", "text": "old prompt"}],
                        }
                    ],
                },
                {
                    "id": "ses_new",
                    "time_created": 100,
                    "messages": [
                        {
                            "role": "user",
                            "time_created": 101,
                            "parts": [{"type": "text", "text": "new prompt"}],
                        }
                    ],
                },
            ],
        )
        # Default: most recent session
        assert parse_opencode(str(db))["first_prompt"] == "new prompt"
        # Explicit argument wins
        assert parse_opencode(str(db), session_id="ses_old")["first_prompt"] == "old prompt"
        # Env var override
        with patch.dict(os.environ, {"MEMENTO_OPENCODE_SESSION_ID": "ses_old"}):
            assert parse_opencode(str(db))["first_prompt"] == "old prompt"

    def test_unknown_session_id_raises(self, opencode_db):
        with pytest.raises(ValueError, match="session not found"):
            parse_opencode(str(opencode_db), session_id="ses_does_not_exist")

    def test_empty_database_raises(self, tmp_path):
        db = tmp_path / "opencode.db"
        _build_opencode_db(db, [])
        with pytest.raises(ValueError, match="no sessions"):
            parse_opencode(str(db))

    def test_dispatcher_routes_to_opencode(self, opencode_db):
        meta = parse_transcript(str(opencode_db))
        assert meta["agent"] == "opencode"
        assert meta["cwd"] == "/home/dev/proj"
        assert meta["files_edited"] == ["/home/dev/proj/auth.py"]

    def test_readonly_open_succeeds_on_readonly_file(self, opencode_db):
        os.chmod(opencode_db, 0o444)
        try:
            meta = parse_opencode(str(opencode_db))
            assert meta["user_messages"] == 2
        finally:
            os.chmod(opencode_db, 0o644)
