"""Tests for memento-triage: parse_transcript, is_substantial, write_fleeting."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

from memento.llm import LLMResult

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
process_structured_notes = _mod.process_structured_notes
run_structured_notes_worker = _mod._run_structured_notes_worker
spawn_memento_agent = _mod.spawn_memento_agent


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
            _assistant_msg(
                [
                    _read_block("/src/a.py"),
                    _read_block("/src/b.py"),
                    _read_block("/src/c.py"),
                    _read_block("/src/d.py"),
                    _read_block("/src/e.py"),
                    _read_block("/src/f.py"),
                ]
            ),
        ]
        meta = parse_transcript(_write_transcript(tmp_path, entries))
        assert len(meta["files_read"]) == 6

    def test_captures_last_outcome(self, tmp_path):
        entries = [
            _user_msg("What's wrong?"),
            _assistant_msg([_text_block("Looking into it...")]),
            _user_msg("Any progress?"),
            _assistant_msg(
                [_text_block("Fixed the null pointer. The issue was an uninitialized variable in the loop.")]
            ),
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


class TestProcessStructuredNotes:
    def test_triage_structured_extraction(self, tmp_vault, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(_user_msg("Figure out the cache bug")) + "\n")

        meta = {
            "cwd": "/home/vic/Projects/api-service",
            "git_branch": "feature/DC-123-cache",
            "exchange_count": 6,
            "files_edited": ["src/cache.py"],
            "first_prompt": "Figure out the cache bug",
            "last_outcome": "Fixed the TTL bug.",
        }

        llm_payload = json.dumps(
            [
                {
                    "title": "Redis cache keys need explicit TTL",
                    "body": "Keys without TTL caused stale reads.",
                    "type": "bugfix",
                    "tags": ["redis", "caching"],
                    "certainty": 3,
                }
            ]
        )

        with (
            patch("memento_triage.get_vault", return_value=tmp_vault),
            patch("memento_triage.llm_complete", return_value=LLMResult(text=llm_payload, ok=True, error=None)),
        ):
            written = process_structured_notes("sess-123", str(transcript), meta, "api-service")

        assert written == 1
        note = tmp_vault / "notes" / "redis-cache-keys-need-explicit-ttl.md"
        assert note.exists()
        assert "type: bugfix" in note.read_text()
        project_file = tmp_vault / "projects" / "api-service.md"
        assert project_file.exists()
        assert "[[redis-cache-keys-need-explicit-ttl]]" in project_file.read_text()

    def test_triage_handles_malformed_json(self, tmp_vault, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(_user_msg("Figure out the cache bug")) + "\n")
        meta = {
            "cwd": "/home/vic/Projects/api-service",
            "git_branch": "feature/DC-123-cache",
            "exchange_count": 6,
            "files_edited": ["src/cache.py"],
            "first_prompt": "Figure out the cache bug",
            "last_outcome": "Fixed the TTL bug.",
        }

        with (
            patch("memento_triage.get_vault", return_value=tmp_vault),
            patch("memento_triage.llm_complete", return_value=LLMResult(text="not json", ok=True, error=None)),
        ):
            written = process_structured_notes("sess-123", str(transcript), meta, "api-service")

        assert written == 0
        assert list((tmp_vault / "notes").glob("*.md")) == []

    def test_triage_logs_parse_empty_details(self, tmp_vault, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(_user_msg("Figure out the cache bug")) + "\n")
        meta = {
            "cwd": "/home/vic/Projects/api-service",
            "git_branch": "feature/DC-123-cache",
            "exchange_count": 6,
            "files_edited": ["src/cache.py"],
            "first_prompt": "Figure out the cache bug",
            "last_outcome": "Fixed the TTL bug.",
        }

        with (
            patch("memento_triage.get_vault", return_value=tmp_vault),
            patch("memento_triage.llm_complete", return_value=LLMResult(text="not json", ok=True, error=None)),
            patch("memento_triage.log_retrieval") as mock_log,
        ):
            written = process_structured_notes("sess-123", str(transcript), meta, "api-service")

        assert written == 0
        mock_log.assert_any_call(
            "triage",
            "structured_notes_parse_empty",
            session_id="sess-123",
            project="api-service",
            raw_preview="not json",
        )


class TestStructuredNotesWorker:
    def test_worker_logs_failure_details(self, tmp_path):
        payload = tmp_path / "payload.json"
        payload.write_text(
            json.dumps(
                {
                    "session_id": "sess-123",
                    "transcript_path": "/tmp/transcript.jsonl",
                    "meta": {"cwd": "/home/vic/Projects/memento-vault", "git_branch": "main"},
                    "project_slug": "memento-vault",
                }
            )
        )
        sentinel = tmp_path / "done.sentinel"

        with (
            patch("memento_triage.process_structured_notes", side_effect=RuntimeError("codex worker boom")),
            patch("memento_triage.log_retrieval") as mock_log,
        ):
            run_structured_notes_worker(str(payload), str(sentinel))

        assert sentinel.exists()
        mock_log.assert_any_call(
            "triage",
            "structured_notes_failed",
            session_id="sess-123",
            error="codex worker boom",
            project="memento-vault",
        )

    def test_worker_logs_empty_result_from_process_structured_notes(self, tmp_path):
        payload = tmp_path / "payload.json"
        payload.write_text(
            json.dumps(
                {
                    "session_id": "sess-123",
                    "transcript_path": "/tmp/transcript.jsonl",
                    "meta": {"cwd": "/home/vic/Projects/memento-vault", "git_branch": "main"},
                    "project_slug": "memento-vault",
                }
            )
        )
        sentinel = tmp_path / "done.sentinel"

        with (
            patch("memento_triage.process_structured_notes", return_value=0),
            patch("memento_triage.log_retrieval") as mock_log,
        ):
            run_structured_notes_worker(str(payload), str(sentinel))

        assert sentinel.exists()
        mock_log.assert_any_call(
            "triage",
            "structured_notes_empty",
            session_id="sess-123",
            project="memento-vault",
        )


class TestSpawnMementoAgent:
    def test_spawn_memento_agent_uses_devnull_stdin(self, tmp_vault, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(_user_msg("Figure out the cache bug")) + "\n")
        meta = {
            "cwd": "/home/vic/Projects/api-service",
            "git_branch": "feature/DC-123-cache",
            "exchange_count": 6,
            "files_edited": ["src/cache.py"],
            "first_prompt": "Figure out the cache bug",
            "last_outcome": "Fixed the TTL bug.",
        }

        with (
            patch("memento_triage.get_vault", return_value=tmp_vault),
            patch("memento_triage.subprocess.Popen") as mock_popen,
        ):
            spawn_memento_agent("sess-1234", str(transcript), meta, "api-service")

        assert mock_popen.call_args.kwargs["stdin"] == _mod.subprocess.DEVNULL

    def test_triage_handles_llm_error(self, tmp_vault, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(_user_msg("Figure out the cache bug")) + "\n")
        meta = {
            "cwd": "/home/vic/Projects/api-service",
            "git_branch": "feature/DC-123-cache",
            "exchange_count": 6,
            "files_edited": ["src/cache.py"],
            "first_prompt": "Figure out the cache bug",
            "last_outcome": "Fixed the TTL bug.",
        }

        with (
            patch("memento_triage.get_vault", return_value=tmp_vault),
            patch("memento_triage.llm_complete", return_value=LLMResult(text="", ok=False, error="boom")),
        ):
            written = process_structured_notes("sess-123", str(transcript), meta, "api-service")

        assert written == 0
        assert list((tmp_vault / "notes").glob("*.md")) == []

    def test_triage_logs_llm_error_details(self, tmp_vault, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(_user_msg("Figure out the cache bug")) + "\n")
        meta = {
            "cwd": "/home/vic/Projects/api-service",
            "git_branch": "feature/DC-123-cache",
            "exchange_count": 6,
            "files_edited": ["src/cache.py"],
            "first_prompt": "Figure out the cache bug",
            "last_outcome": "Fixed the TTL bug.",
        }

        with (
            patch("memento_triage.get_vault", return_value=tmp_vault),
            patch("memento_triage.llm_complete", return_value=LLMResult(text="", ok=False, error="codex timed out")),
            patch("memento_triage.log_retrieval") as mock_log,
        ):
            written = process_structured_notes("sess-123", str(transcript), meta, "api-service")

        assert written == 0
        mock_log.assert_any_call(
            "triage",
            "structured_notes_llm_failed",
            session_id="sess-123",
            project="api-service",
            error="codex timed out",
        )

    def test_triage_logs_lock_timeout_details(self, tmp_vault, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(_user_msg("Figure out the cache bug")) + "\n")
        meta = {
            "cwd": "/home/vic/Projects/api-service",
            "git_branch": "feature/DC-123-cache",
            "exchange_count": 6,
            "files_edited": ["src/cache.py"],
            "first_prompt": "Figure out the cache bug",
            "last_outcome": "Fixed the TTL bug.",
        }
        llm_payload = json.dumps(
            [
                {
                    "title": "Redis cache keys need explicit TTL",
                    "body": "Keys without TTL caused stale reads.",
                    "type": "bugfix",
                    "tags": ["redis", "caching"],
                    "certainty": 3,
                }
            ]
        )

        with (
            patch("memento_triage.get_vault", return_value=tmp_vault),
            patch("memento_triage.llm_complete", return_value=LLMResult(text=llm_payload, ok=True, error=None)),
            patch("memento_triage.acquire_vault_write_lock", return_value=False),
            patch("memento_triage.log_retrieval") as mock_log,
        ):
            written = process_structured_notes("sess-123", str(transcript), meta, "api-service")

        assert written == 0
        mock_log.assert_any_call(
            "triage",
            "structured_notes_lock_timeout",
            session_id="sess-123",
            project="api-service",
        )

    def test_triage_sanitizes_secrets_in_note_body(self, tmp_vault, tmp_path):
        """Regression: note bodies must be sanitized before writing to vault."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(_user_msg("Fix the auth bug")) + "\n")
        meta = {
            "cwd": "/home/vic/Projects/api-service",
            "git_branch": "main",
            "exchange_count": 6,
            "files_edited": ["src/auth.py"],
            "first_prompt": "Fix the auth bug",
            "last_outcome": "Fixed it.",
        }
        llm_payload = json.dumps(
            [
                {
                    "title": "Auth token handling",
                    "body": "Used token sk-abcdefghij1234567890abcdefghij to authenticate.",
                    "type": "discovery",
                    "tags": ["auth"],
                    "certainty": 3,
                }
            ]
        )

        with (
            patch("memento_triage.get_vault", return_value=tmp_vault),
            patch("memento_triage.llm_complete", return_value=LLMResult(text=llm_payload, ok=True, error=None)),
        ):
            written = process_structured_notes("sess-123", str(transcript), meta, "api-service")

        assert written == 1
        note_files = list((tmp_vault / "notes").glob("*.md"))
        assert len(note_files) == 1
        note_text = note_files[0].read_text()
        assert "sk-abcdefghij1234567890abcdefghij" not in note_text

    def test_triage_sanitizes_transcript_before_llm(self, tmp_vault, tmp_path):
        """Regression: transcript sent to LLM must be sanitized."""
        secret = "AKIA1234567890ABCDEF"
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(_user_msg(f"Deploy with key {secret}")) + "\n")
        meta = {
            "cwd": "/home/vic/Projects/api-service",
            "git_branch": "main",
            "exchange_count": 6,
            "files_edited": ["deploy.py"],
            "first_prompt": "Deploy",
            "last_outcome": "Deployed.",
        }

        captured_prompt = {}

        def mock_llm(prompt, config=None):
            captured_prompt["text"] = prompt
            return LLMResult(text="[]", ok=True, error=None)

        with (
            patch("memento_triage.get_vault", return_value=tmp_vault),
            patch("memento_triage.llm_complete", side_effect=mock_llm),
        ):
            process_structured_notes("sess-123", str(transcript), meta, "api-service")

        assert secret not in captured_prompt["text"]

    def test_triage_logs_transcript_read_failure(self, tmp_vault, tmp_path):
        """Regression: unreadable transcript must log, not silently return 0."""
        meta = {
            "cwd": "/home/vic/Projects/api-service",
            "git_branch": "main",
            "exchange_count": 6,
            "files_edited": ["src/auth.py"],
            "first_prompt": "Fix it",
            "last_outcome": "Fixed.",
        }

        with (
            patch("memento_triage.get_vault", return_value=tmp_vault),
            patch("memento_triage.log_retrieval") as mock_log,
        ):
            written = process_structured_notes("sess-123", "/nonexistent/transcript.jsonl", meta, "api-service")

        assert written == 0
        mock_log.assert_any_call(
            "triage",
            "structured_notes_transcript_unreadable",
            session_id="sess-123",
            project="api-service",
        )
