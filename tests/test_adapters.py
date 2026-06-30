"""Tests for transcript parsing adapters."""

import json
import os
import sqlite3
import uuid
from unittest.mock import patch

import pytest

from memento.adapters import detect_agent, parse_transcript, render_transcript_text, truncate_transcript
from memento.adapters.claude import parse_transcript as parse_claude
from memento.adapters.opencode import parse_transcript as parse_opencode
from memento.adapters.pi import parse_transcript as parse_pi
from memento.adapters.pi import render_transcript_text as render_pi


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

    def test_detects_pi_from_message_records(self, tmp_path):
        transcript = tmp_path / "pi-session.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "message",
                    "message": {"role": "user", "content": [{"type": "text", "text": "remember this"}]},
                }
            )
            + "\n"
        )
        assert detect_agent(str(transcript)) == "pi"

    def test_detects_pi_from_streaming_session_records(self, tmp_path):
        transcript = tmp_path / "pi-stream.jsonl"
        transcript.write_text(
            json.dumps({"type": "session", "version": 3, "id": "pi-s1", "cwd": "/repo"})
            + "\n"
            + json.dumps(
                {
                    "type": "message_start",
                    "message": {"role": "user", "content": [{"type": "text", "text": "remember this"}]},
                }
            )
            + "\n"
        )
        assert detect_agent(str(transcript)) == "pi"


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

    def test_dispatcher_routes_to_pi(self, tmp_path):
        transcript = _write_pi_transcript(tmp_path)
        meta = parse_transcript(str(transcript))
        assert meta["agent"] == "pi"
        assert meta["first_prompt"] == "Fix the Pi transcript adapter"
        assert "memento/adapters/pi.py" in meta["files_edited"]


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

    def test_tool_result_round_trips_do_not_count_as_user_exchanges(self, tmp_path):
        transcript = tmp_path / "tool-heavy.jsonl"
        lines = [
            json.dumps(
                {
                    "type": "user",
                    "cwd": "/repo/memento",
                    "gitBranch": "mem-56",
                    "message": {"content": "Investigate the failing test"},
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "I'll inspect the failure."},
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"file_path": "/repo/memento/tests/test_a.py"},
                            },
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "first result"},
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "/repo/memento/memento/a.py"}},
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "toolu_2", "content": "second result"},
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Found the root cause."}]},
                }
            ),
        ]
        transcript.write_text("\n".join(lines))

        meta = parse_claude(str(transcript))

        assert meta["exchange_count"] == 1
        assert meta["user_messages"] == 1
        assert meta["first_prompt"] == "Investigate the failing test"
        assert meta["files_read"] == ["/repo/memento/memento/a.py", "/repo/memento/tests/test_a.py"]


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

    def test_extracts_apply_patch_files(self, tmp_path):
        db = tmp_path / "opencode.db"
        _build_opencode_db(
            db,
            [
                {
                    "id": "ses_patch",
                    "time_created": 1,
                    "messages": [
                        {
                            "role": "user",
                            "time_created": 2,
                            "parts": [{"type": "text", "text": "patch several files"}],
                        },
                        {
                            "role": "assistant",
                            "time_created": 3,
                            "parts": [
                                {
                                    "type": "tool",
                                    "tool": "apply_patch",
                                    "state": {
                                        "input": {
                                            "patchText": """*** Begin Patch
*** Add File: new.py
+print('new')
*** Update File: existing.py
@@
-old
+new
*** Delete File: old.py
*** Update File: renamed.py
*** Move to: moved.py
@@
-content
+content
*** End Patch"""
                                        }
                                    },
                                }
                            ],
                        },
                    ],
                }
            ],
        )
        meta = parse_opencode(str(db))
        assert meta["files_edited"] == ["existing.py", "moved.py", "new.py", "old.py"]

    def test_first_prompt_and_outcome(self, opencode_db):
        meta = parse_opencode(str(opencode_db))
        assert meta["first_prompt"] == "Fix the broken login flow"
        assert meta["last_outcome"] == "Done."

    def test_unwraps_whole_prompt_wrapper(self, tmp_path):
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
                            "parts": [{"type": "text", "text": "<system-reminder>Do the thing</system-reminder>"}],
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

    def test_preserves_user_markup_in_prompt(self, tmp_path):
        db = tmp_path / "opencode.db"
        _build_opencode_db(
            db,
            [
                {
                    "id": "ses_markup",
                    "time_created": 1,
                    "messages": [
                        {
                            "role": "user",
                            "time_created": 2,
                            "parts": [
                                {
                                    "type": "text",
                                    "text": "Explain <div>hello</div> and <system>role tags</system>",
                                }
                            ],
                        }
                    ],
                }
            ],
        )
        meta = parse_opencode(str(db))
        assert meta["first_prompt"] == "Explain <div>hello</div> and <system>role tags</system>"

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
        # Explicit argument wins when env var is unset
        assert parse_opencode(str(db), session_id="ses_old")["first_prompt"] == "old prompt"
        # Env var overrides explicit argument
        with patch.dict(os.environ, {"MEMENTO_OPENCODE_SESSION_ID": "ses_new"}):
            assert parse_opencode(str(db), session_id="ses_old")["first_prompt"] == "new prompt"

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


# --- Pi adapter ---


def _write_pi_transcript(tmp_path, records=None):
    transcript = tmp_path / "pi-session.jsonl"
    records = records or [
        {
            "type": "session",
            "session_id": "pi-s1",
            "cwd": "/repo/memento-vault",
            "gitBranch": "vic/mem-41",
        },
        {
            "type": "message",
            "timestamp": "2026-06-14T00:00:00Z",
            "cwd": "/repo/memento-vault",
            "gitBranch": "vic/mem-41",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Fix the Pi transcript adapter"}],
            },
        },
        {
            "type": "message",
            "timestamp": "2026-06-14T00:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden chain of thought", "encrypted_content": "opaque"},
                    {"type": "text", "text": "I'll add the adapter."},
                    {"type": "toolCall", "name": "read", "arguments": {"file_path": "memento/adapters/__init__.py"}},
                    {"type": "toolCall", "name": "edit", "arguments": {"file_path": "memento/adapters/pi.py"}},
                ],
            },
        },
        {
            "type": "message",
            "timestamp": "2026-06-14T00:00:02Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "ship it"}]},
        },
        {
            "type": "message",
            "timestamp": "2026-06-14T00:00:03Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done. Pi transcripts now render safely."}],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return transcript


class TestPiAdapter:
    def test_parses_metadata_and_files(self, tmp_path):
        transcript = _write_pi_transcript(tmp_path)
        meta = parse_pi(str(transcript))
        assert meta["cwd"] == "/repo/memento-vault"
        assert meta["git_branch"] == "vic/mem-41"
        assert meta["exchange_count"] == 2
        assert meta["user_messages"] == 2
        assert meta["files_read"] == ["memento/adapters/__init__.py"]
        assert meta["files_edited"] == ["memento/adapters/pi.py"]
        assert meta["first_prompt"] == "Fix the Pi transcript adapter"
        assert meta["last_outcome"] == "Done."
        assert meta["session_id"] == "pi-s1"

    def test_parses_streaming_pi_session_shape(self, tmp_path):
        transcript = _write_pi_transcript(
            tmp_path,
            [
                {"type": "session", "version": 3, "id": "pi-s1", "cwd": "/repo/memento-vault"},
                {
                    "type": "message_start",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": '<file name="prompt.md">Fix the adapter</file>'}],
                    },
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": '<file name="prompt.md">Fix the adapter</file>'}],
                    },
                },
                {
                    "type": "message_update",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "thinking", "thinking": "private reasoning"}],
                    },
                },
                {
                    "type": "tool_execution_start",
                    "toolName": "read",
                    "args": {"path": "memento/adapters/__init__.py"},
                },
                {
                    "type": "tool_execution_start",
                    "toolName": "write",
                    "args": {"path": "memento/adapters/pi.py"},
                },
                {
                    "type": "tool_execution_end",
                    "toolName": "read",
                    "result": {"content": [{"type": "text", "text": "x" * 20}]},
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "secret"},
                            {"type": "text", "text": "Done. Adapter supports Pi event streams."},
                        ],
                    },
                },
            ],
        )

        meta = parse_pi(str(transcript))
        rendered = render_pi(str(transcript), per_tool_cap=5)

        assert meta["cwd"] == "/repo/memento-vault"
        assert meta["session_id"] == "pi-s1"
        assert meta["exchange_count"] == 1
        assert meta["user_messages"] == 1
        assert meta["files_read"] == ["memento/adapters/__init__.py"]
        assert meta["files_edited"] == ["memento/adapters/pi.py"]
        assert meta["first_prompt"] == "Fix the adapter"
        assert meta["last_outcome"] == "Done."
        assert "User: <file" in rendered
        assert "Assistant tool read: memento/adapters/__init__.py" in rendered
        assert "Assistant tool write: memento/adapters/pi.py" in rendered
        assert "Tool result read: xxxxx" in rendered
        assert "[tool result truncated]" in rendered
        assert "private reasoning" not in rendered
        assert "secret" not in rendered

    def test_render_transcript_text_deduplicates_finalized_tool_events(self, tmp_path):
        tool_call_id = "call-1"
        transcript = _write_pi_transcript(
            tmp_path,
            [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": tool_call_id,
                                "name": "read",
                                "arguments": {"path": "memento/adapters/pi.py"},
                            }
                        ],
                    },
                },
                {
                    "type": "tool_execution_start",
                    "toolName": "read",
                    "toolCallId": tool_call_id,
                    "args": {"path": "memento/adapters/pi.py"},
                },
                {
                    "type": "tool_execution_end",
                    "toolName": "read",
                    "toolCallId": tool_call_id,
                    "result": {"content": [{"type": "text", "text": "x" * 20}]},
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "toolResult",
                        "toolName": "read",
                        "toolCallId": tool_call_id,
                        "content": [{"type": "text", "text": "x" * 20}],
                    },
                },
            ],
        )

        rendered = render_pi(str(transcript), per_tool_cap=5)

        assert rendered.count("Assistant tool read: memento/adapters/pi.py") == 1
        assert rendered.count("Tool result read: xxxxx") == 1
        assert "xxxxxxxxxx" not in rendered
        assert "[tool result truncated]" in rendered

    def test_render_transcript_text_excludes_thinking_and_caps_tool_results(self, tmp_path):
        transcript = _write_pi_transcript(
            tmp_path,
            [
                {
                    "type": "message",
                    "message": {"role": "user", "content": [{"type": "text", "text": "Capture durable signal"}]},
                },
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "reasoning", "text": "private reasoning"},
                            {"type": "thinking", "thinking": "secret reasoning", "thinkingSignature": "sig"},
                            {"type": "toolCall", "name": "memento_capture", "arguments": {"title": "Decision"}},
                        ],
                    },
                },
                {
                    "type": "message",
                    "message": {"role": "toolResult", "content": [{"type": "toolResult", "text": "x" * 20}]},
                },
                {
                    "type": "message",
                    "message": {"role": "toolResult", "content": [{"type": "text", "text": "y" * 20}]},
                },
            ],
        )
        rendered = render_pi(str(transcript), per_tool_cap=5)
        assert "User: Capture durable signal" in rendered
        assert "Assistant tool memento_capture" in rendered
        assert "Tool: xxxxx" in rendered
        assert "Tool: yyyyy" in rendered
        assert "yyyyyyyyyy" not in rendered
        assert "[tool result truncated]" in rendered
        assert "private reasoning" not in rendered
        assert "secret reasoning" not in rendered
        assert "thinkingSignature" not in rendered

    def test_dispatcher_render_uses_pi_renderer(self, tmp_path):
        transcript = _write_pi_transcript(tmp_path)
        rendered = render_transcript_text(str(transcript), agent="pi")
        assert "User: Fix the Pi transcript adapter" in rendered
        assert "Assistant: I'll add the adapter." in rendered
        assert "hidden chain of thought" not in rendered

    def test_empty_transcript_is_predictable(self, tmp_path):
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")
        meta = parse_pi(str(transcript))
        assert meta["exchange_count"] == 0
        assert meta["files_edited"] == []
        assert render_pi(str(transcript)) == ""

    def test_malformed_jsonl_records_are_skipped(self, tmp_path):
        transcript = tmp_path / "partial.jsonl"
        transcript.write_text(
            json.dumps({"type": "session", "id": "pi-s1", "cwd": "/repo"})
            + "\n"
            + '{"type":"message",'
            + "\n"
            + json.dumps(
                {
                    "type": "message",
                    "message": {"role": "user", "content": [{"type": "text", "text": "keep this"}]},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        meta = parse_pi(str(transcript))
        rendered = render_pi(str(transcript))

        assert meta["session_id"] == "pi-s1"
        assert meta["first_prompt"] == "keep this"
        assert "User: keep this" in rendered

    def test_oversize_render_can_be_truncated_by_shared_budget(self, tmp_path):
        records = [
            {
                "type": "message",
                "message": {"role": "user", "content": [{"type": "text", "text": "start " + "a" * 5000}]},
            },
            {
                "type": "message",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "end " + "z" * 5000}]},
            },
        ]
        transcript = _write_pi_transcript(tmp_path, records)
        rendered = render_pi(str(transcript))
        truncated = truncate_transcript(rendered, 1000)
        assert len(truncated) <= 1000
        assert "transcript truncated" in truncated
        assert truncated.startswith("User: start")
        assert truncated.endswith("z" * 100)


class TestTruncateTranscript:
    def test_under_budget_unchanged(self):
        text = "line one\nline two\nline three\n"
        assert truncate_transcript(text, 1000) is text

    def test_zero_budget_disables_truncation(self):
        text = "x" * 10_000
        assert truncate_transcript(text, 0) is text

    def test_over_budget_truncates_to_max(self):
        lines = [f"line {i} " + "x" * 90 for i in range(2000)]
        text = "\n".join(lines)
        result = truncate_transcript(text, 50_000)

        assert len(result) <= 50_000
        assert "transcript truncated" in result
        assert "characters elided from the middle" in result

    def test_keeps_head_and_tail(self):
        lines = [f"line {i} " + "x" * 90 for i in range(2000)]
        text = "\n".join(lines)
        result = truncate_transcript(text, 50_000)

        assert result.startswith("line 0 ")
        assert result.rstrip().endswith(lines[-1])
        # The tail carries outcomes and decisions, so it gets the larger share.
        marker_pos = result.index("transcript truncated")
        assert marker_pos < len(result) - marker_pos

    def test_cuts_on_line_boundaries(self):
        lines = [f"record-{i:06d}" for i in range(10_000)]
        text = "\n".join(lines)
        result = truncate_transcript(text, 20_000)

        head, _, rest = result.partition("\n[... transcript truncated")
        _, _, tail = rest.partition("...]\n")
        for chunk in head.strip().splitlines() + tail.strip().splitlines():
            assert chunk in lines

    def test_tiny_budget_falls_back_to_tail(self):
        text = "x" * 10_000
        result = truncate_transcript(text, 100)

        assert len(result) == 100
        assert result == text[-100:]


class TestLazyAdapterLoading:
    """Optional adapters (opencode, pi) may be absent on a Claude-only install.

    The opencode and pi modules are optional transcript backends that may be
    absent. Their absence must never crash the dispatcher at import or detection
    time; it may surface only as a clear error if such a transcript is actually
    parsed. This guards against the regression where a top-level import of a
    missing adapter took the whole triage hook down before it could process
    Claude transcripts.
    """

    def test_load_adapter_returns_none_on_import_error(self):
        import memento.adapters as adapters_mod

        with patch.object(adapters_mod.importlib, "import_module", side_effect=ImportError("absent")):
            assert adapters_mod._load_adapter("opencode") is None
            assert adapters_mod._load_adapter("pi") is None

    def test_detect_falls_back_to_claude_when_adapters_absent(self, claude_transcript):
        with patch("memento.adapters._load_adapter", return_value=None):
            assert detect_agent(str(claude_transcript)) == "claude"

    @pytest.mark.parametrize("agent", ["opencode", "pi"])
    def test_parse_missing_adapter_raises_clear_error(self, claude_transcript, agent):
        with patch("memento.adapters._load_adapter", return_value=None):
            with pytest.raises(ValueError, match=f"{agent} adapter is not installed"):
                parse_transcript(str(claude_transcript), agent=agent)

    @pytest.mark.parametrize("agent", ["opencode", "pi"])
    def test_render_missing_adapter_raises_clear_error(self, claude_transcript, agent):
        with patch("memento.adapters._load_adapter", return_value=None):
            with pytest.raises(ValueError, match=f"{agent} adapter is not installed"):
                render_transcript_text(str(claude_transcript), agent=agent)
