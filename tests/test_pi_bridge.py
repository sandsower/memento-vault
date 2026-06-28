import json
import os
from pathlib import Path
from unittest.mock import patch

from memento.lifecycle import LifecycleResult
from memento import pi_bridge


def test_pi_bridge_recall_outputs_lifecycle_json(capsys):
    result = LifecycleResult(True, "[vault] Related memories:", "recall", results=[{"path": "notes/a.md"}])

    with patch("memento.pi_bridge.build_recall", return_value=result) as mock_build:
        code = pi_bridge.main(["recall", "--prompt", "What changed?", "--cwd", "/repo", "--session-id", "s1"])

    assert code == 0
    mock_build.assert_called_once_with("What changed?", "/repo", "s1")
    assert json.loads(capsys.readouterr().out) == result.to_dict()


def test_pi_bridge_tool_context_outputs_lifecycle_json(capsys):
    result = LifecycleResult(False, "", "tool-context", reason="unsupported-tool")

    with patch("memento.pi_bridge.build_tool_context", return_value=result) as mock_build:
        code = pi_bridge.main(
            [
                "tool-context",
                "--tool-name",
                "bash",
                "--file-path",
                "src/a.py",
                "--cwd",
                "/repo",
                "--session-id",
                "s1",
            ]
        )

    assert code == 0
    mock_build.assert_called_once_with("bash", "src/a.py", "/repo", "s1")
    assert json.loads(capsys.readouterr().out) == result.to_dict()


def test_pi_bridge_session_context_outputs_json(capsys):
    expected = {
        "should_inject": True,
        "content": "[vault] Project: repo",
        "source": "session-context",
        "sections": {},
        "results": [],
        "metadata": {"truncated": False},
    }

    with patch("memento.pi_bridge.build_session_context", return_value=expected) as mock_build:
        code = pi_bridge.main(
            [
                "session-context",
                "--cwd",
                "/repo",
                "--prompt",
                "cache",
                "--session-id",
                "s1",
                "--token-budget",
                "500",
                "--include-tool-context-preview",
            ]
        )

    assert code == 0
    assert json.loads(capsys.readouterr().out) == expected
    mock_build.assert_called_once_with("/repo", "cache", "s1", 500, True, True, True, True)


def test_pi_bridge_status_outputs_json(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    with (
        patch("memento.pi_bridge.get_vault", return_value=tmp_path),
        patch("memento.pi_bridge.has_qmd", return_value=False),
        patch("memento.pi_bridge.detect_project", return_value=("repo", None)),
        patch("memento.pi_bridge.get_config", return_value={}),
    ):
        code = pi_bridge.main(["status", "--cwd", "/repo"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["vault_path"] == str(tmp_path)
    assert payload["project_slug"] == "repo"
    assert payload["qmd_available"] is False
    assert payload["queued_capture_count"] == 0


def test_pi_bridge_search_reports_backend_unavailable_with_miss(capsys):
    with (
        patch("memento.pi_bridge.has_qmd", return_value=False),
        patch("memento.pi_bridge.is_remote", return_value=False),
    ):
        code = pi_bridge.main(["search", "--query", "cache"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "results": [],
        "miss": {
            "reason": "backend_unavailable",
            "recovery_hints": [
                "Check memento_status for search backend health.",
                "Run memento_reindex if the index is stale.",
            ],
        },
        "reason": "backend_unavailable",
    }


def test_pi_bridge_search_preserves_remote_miss(capsys):
    remote_miss = {
        "results": [],
        "miss": {"reason": "threshold_too_high", "recovery_hints": ["Lower min_score."]},
    }
    with (
        patch("memento.pi_bridge.has_qmd", return_value=False),
        patch("memento.pi_bridge.is_remote", return_value=True),
        patch("memento.pi_bridge.remote_search_envelope", return_value=remote_miss),
    ):
        code = pi_bridge.main(["search", "--query", "cache"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "results": [],
        "miss": {"reason": "threshold_too_high", "recovery_hints": ["Lower min_score."]},
        "reason": "threshold_too_high",
    }


def test_pi_bridge_search_reports_remote_error_as_backend_unavailable(capsys):
    with (
        patch("memento.pi_bridge.has_qmd", return_value=False),
        patch("memento.pi_bridge.is_remote", return_value=True),
        patch("memento.pi_bridge.remote_search_envelope", return_value={"results": [], "error": "boom"}),
    ):
        code = pi_bridge.main(["search", "--query", "cache"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"] == []
    assert payload["reason"] == "backend_unavailable"
    assert payload["miss"]["reason"] == "backend_unavailable"
    assert payload["miss"]["details"] == {"error": "boom"}


def test_pi_bridge_search_forwards_concrete_option(capsys):
    with (
        patch("memento.pi_bridge.has_qmd", return_value=True),
        patch(
            "memento.pi_bridge.qmd_search_with_extras",
            return_value=[{"path": "notes/env.md", "title": "Env", "score": 1.0, "snippet": "MEMENTO_VAULT_PATH"}],
        ) as mock_search,
        patch("memento.pi_bridge.enhance_results") as mock_enhance,
    ):
        code = pi_bridge.main(["search", "--query", "MEMENTO_VAULT_PATH", "--concrete", "true"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["path"] == "notes/env.md"
    assert mock_search.call_args.kwargs["concrete"] is True
    mock_enhance.assert_not_called()


def test_pi_bridge_search_reports_project_filter_removed_all(capsys):
    with (
        patch("memento.pi_bridge.has_qmd", return_value=True),
        patch(
            "memento.pi_bridge.qmd_search_with_extras",
            return_value=[{"path": "notes/dala.md", "title": "Dala", "score": 0.9, "snippet": ""}],
        ),
        patch("memento.pi_bridge.enhance_results", return_value=[]),
    ):
        code = pi_bridge.main(["search", "--query", "fundid email", "--cwd", "/repo/fundid"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"] == []
    assert payload["reason"] == "project_filter_removed_all"
    assert payload["miss"]["details"] == {"cwd": "/repo/fundid"}


def test_pi_bridge_capture_writes_manual_note(capsys, tmp_path):
    (tmp_path / "notes").mkdir()
    with (
        patch("memento.pi_bridge.get_vault", return_value=tmp_path),
        patch("memento.pi_bridge.detect_project", return_value=("repo", None)),
    ):
        code = pi_bridge.main(
            [
                "capture",
                "--title",
                "Pi bridge",
                "--body",
                "Lifecycle bridge works",
                "--cwd",
                "/repo",
                "--session-id",
                "s1",
            ]
        )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"].startswith("notes/pi-bridge")
    note_text = (tmp_path / payload["path"]).read_text()
    assert "type: discovery" in note_text
    assert 'tags: ["pi", "manual", "repo"]' in note_text
    assert "source: pi-capture" in note_text
    assert "origin: pi_bridge:manual" in note_text
    assert "certainty: 2" in note_text
    assert "project: /repo" in note_text


def test_pi_bridge_capture_records_manual_session_state(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "notes").mkdir()
    with (
        patch("memento.pi_bridge.get_vault", return_value=tmp_path),
        patch("memento.pi_bridge.detect_project", return_value=("repo", None)),
        patch("memento.pi_bridge._git_branch", return_value="feature/pi"),
    ):
        code = pi_bridge.main(
            [
                "capture",
                "--title",
                "Manual capture",
                "--body",
                "Durable decision captured by the user.",
                "--cwd",
                "/repo",
                "--session-id",
                "s1",
            ]
        )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["queued"] is False
    state_files = list((tmp_path / "state" / "capture-sessions").glob("*.json"))
    assert len(state_files) == 1
    state = json.loads(state_files[0].read_text())
    assert state["manual_capture_at"]
    assert state["session_id"] == "s1"
    assert state["cwd"] == "/repo"
    assert state["project"] == "repo"
    assert state["branch"] == "feature/pi"


def test_pi_bridge_session_state_write_failure_is_nonfatal(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
        pi_bridge._write_capture_session_state("s1", "/repo", {"session_id": "s1"})

    assert "could not write pi capture session state" in capsys.readouterr().err


def test_pi_bridge_lifecycle_after_manual_capture_skips_low_signal_body(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "notes").mkdir()
    with (
        patch("memento.pi_bridge.get_vault", return_value=tmp_path),
        patch("memento.pi_bridge.detect_project", return_value=("repo", None)),
        patch("memento.pi_bridge._git_branch", return_value="feature/pi"),
    ):
        assert (
            pi_bridge.main(
                [
                    "capture",
                    "--title",
                    "Manual",
                    "--body",
                    "Captured the important point.",
                    "--cwd",
                    "/repo",
                    "--session-id",
                    "s1",
                ]
            )
            == 0
        )
        capsys.readouterr()
        code = pi_bridge.main(
            [
                "capture",
                "--title",
                "Pi session candidate capture",
                "--body",
                "- user: thanks\n- assistant: done",
                "--cwd",
                "/repo",
                "--session-id",
                "s1",
                "--queue",
                "--reason",
                "agent_end",
                "--source-event",
                "agent_end",
            ]
        )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "queued": False,
        "skipped": True,
        "reason": "manual_capture_suppressed_lifecycle",
        "source_event": "agent_end",
        "session_id": "s1",
    }
    assert not (tmp_path / "state" / "queue" / "pi-captures.jsonl").exists()


def test_pi_bridge_lifecycle_after_manual_capture_queues_meaningful_keyword(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "notes").mkdir()
    with (
        patch("memento.pi_bridge.get_vault", return_value=tmp_path),
        patch("memento.pi_bridge.detect_project", return_value=("repo", None)),
        patch("memento.pi_bridge._git_branch", return_value="feature/pi"),
    ):
        assert (
            pi_bridge.main(
                [
                    "capture",
                    "--title",
                    "Manual",
                    "--body",
                    "Captured earlier point.",
                    "--cwd",
                    "/repo",
                    "--session-id",
                    "s1",
                ]
            )
            == 0
        )
        capsys.readouterr()
        code = pi_bridge.main(
            [
                "capture",
                "--title",
                "Pi session candidate capture",
                "--body",
                "- user: The root cause is the lifecycle queue gate.",
                "--cwd",
                "/repo",
                "--session-id",
                "s1",
                "--queue",
                "--reason",
                "agent_end",
                "--source-event",
                "agent_end",
            ]
        )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["queued"] is True
    assert (tmp_path / "state" / "queue" / "pi-captures.jsonl").exists()


def test_pi_bridge_lifecycle_after_manual_capture_queues_exchange_threshold(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "notes").mkdir()
    body = "\n".join(["- user: one", "- assistant: two", "- user: three", "- assistant: four", "- user: five"])
    with (
        patch("memento.pi_bridge.get_vault", return_value=tmp_path),
        patch("memento.pi_bridge.detect_project", return_value=("repo", None)),
        patch("memento.pi_bridge._git_branch", return_value="feature/pi"),
    ):
        assert (
            pi_bridge.main(
                [
                    "capture",
                    "--title",
                    "Manual",
                    "--body",
                    "Captured earlier point.",
                    "--cwd",
                    "/repo",
                    "--session-id",
                    "s1",
                ]
            )
            == 0
        )
        capsys.readouterr()
        code = pi_bridge.main(
            [
                "capture",
                "--title",
                "Pi session candidate capture",
                "--body",
                body,
                "--cwd",
                "/repo",
                "--session-id",
                "s1",
                "--queue",
                "--reason",
                "agent_end",
                "--source-event",
                "agent_end",
            ]
        )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["queued"] is True


def test_pi_bridge_manual_queued_capture_bypasses_lifecycle_gate(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "notes").mkdir()
    with (
        patch("memento.pi_bridge.get_vault", return_value=tmp_path),
        patch("memento.pi_bridge.detect_project", return_value=("repo", None)),
        patch("memento.pi_bridge._git_branch", return_value="feature/pi"),
    ):
        assert (
            pi_bridge.main(
                [
                    "capture",
                    "--title",
                    "Manual",
                    "--body",
                    "Captured earlier point.",
                    "--cwd",
                    "/repo",
                    "--session-id",
                    "s1",
                ]
            )
            == 0
        )
        capsys.readouterr()
        code = pi_bridge.main(
            [
                "capture",
                "--title",
                "Manual queued capture",
                "--body",
                "Queue this explicitly.",
                "--cwd",
                "/repo",
                "--session-id",
                "s1",
                "--queue",
                "--reason",
                "manual",
                "--source-event",
                "tool",
            ]
        )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["queued"] is True


def test_pi_bridge_lifecycle_without_manual_baseline_still_queues(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    with (
        patch("memento.pi_bridge.get_vault", return_value=tmp_path),
        patch("memento.pi_bridge.detect_project", return_value=("repo", None)),
        patch("memento.pi_bridge._git_branch", return_value="feature/pi"),
    ):
        code = pi_bridge.main(
            [
                "capture",
                "--title",
                "Pi session candidate capture",
                "--body",
                "- user: hello\n- assistant: helped",
                "--cwd",
                "/repo",
                "--session-id",
                "s1",
                "--queue",
                "--reason",
                "agent_end",
                "--source-event",
                "agent_end",
            ]
        )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["queued"] is True


def test_pi_bridge_capture_can_queue_instead_of_write(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    with (
        patch("memento.pi_bridge.get_vault", return_value=tmp_path),
        patch("memento.pi_bridge.detect_project", return_value=("repo", None)),
        patch("memento.pi_bridge._git_branch", return_value="feature/pi"),
    ):
        code = pi_bridge.main(
            [
                "capture",
                "--title",
                "Queued pi capture",
                "--body",
                "Review this before storing.",
                "--cwd",
                "/repo",
                "--session-id",
                "s1",
                "--queue",
                "--reason",
                "agent_end",
                "--source-event",
                "agent_end",
            ]
        )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["queued"] is True
    assert payload["id"]
    assert not (tmp_path / "notes").exists()

    queue_lines = (tmp_path / "state" / "queue" / "pi-captures.jsonl").read_text().splitlines()
    queued = json.loads(queue_lines[0])
    assert queued["title"] == "Queued pi capture"
    assert queued["metadata"]["project"] == "repo"
    assert queued["metadata"]["branch"] == "feature/pi"
    assert queued["metadata"]["session_id"] == "s1"


def test_pi_bridge_capture_writes_type_tags_certainty_and_session_metadata_as_frontmatter(
    capsys, tmp_path, monkeypatch
):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    with (
        patch("memento.pi_bridge.get_vault", return_value=tmp_path),
        patch("memento.pi_bridge.detect_project", return_value=("repo", None)),
        patch("memento.pi_bridge._git_branch", return_value="feature/pi"),
    ):
        code = pi_bridge.main(
            [
                "capture",
                "--title",
                "Typed pi memory",
                "--body",
                "Use the deterministic dedup packet before creating notes.",
                "--cwd",
                "/repo",
                "--session-id",
                "/Users/vic/.pi/agent/sessions/session.jsonl",
                "--note-type",
                "decision",
                "--branch",
                "original/pi-branch",
                "--tag",
                "dedup",
                "--tag",
                "curation",
                "--certainty",
                "4",
            ]
        )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    note = tmp_path / payload["path"]
    text = note.read_text()
    frontmatter, body = text.split("---", 2)[1:]
    assert "type: decision" in frontmatter
    assert 'tags: ["pi", "manual", "repo", "dedup", "curation"]' in frontmatter
    assert "certainty: 4" in frontmatter
    assert "project: /repo" in frontmatter
    assert "branch: original/pi-branch" in frontmatter
    assert "session_id: /Users/vic/.pi/agent/sessions/session.jsonl" in frontmatter
    assert "/Users/vic/.pi/agent/sessions/session.jsonl" not in body
    assert "Session ID:" not in body


def test_pi_bridge_queue_migrates_to_local_state_and_processes(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    legacy_queue_file = tmp_path / "queue" / "pi-captures.jsonl"
    legacy_queue_file.parent.mkdir()
    legacy_queue_file.write_text(
        json.dumps(
            {
                "id": "q1",
                "title": "Queued pi capture",
                "body": "Review this before storing.",
                "metadata": {"project": "repo", "branch": "feature/pi", "session_id": "s1"},
            }
        )
        + "\n"
    )

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "list"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["captures"][0]["id"] == "q1"
    assert "body" not in payload["captures"][0]
    assert not legacy_queue_file.exists()
    local_queue_file = tmp_path / "state" / "queue" / "pi-captures.jsonl"
    assert local_queue_file.exists()

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "process-start", "--project", "repo", "--dry-run"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["selected_capture_count"] == 1
    assert payload["group_count"] == 1
    assert payload["groups"][0]["capture_ids"] == ["q1"]


def test_pi_bridge_process_start_rejects_active_lock(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    queue_file = tmp_path / "state" / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps(
            {
                "id": "q1",
                "title": "One",
                "body": "A",
                "metadata": {"project": "repo", "branch": "b", "session_id": "s1"},
            }
        )
        + "\n"
    )
    lock_file = tmp_path / "state" / "processing.lock"
    lock_file.write_text(json.dumps({"run_id": "active", "pid": 1, "created_time": 9999999999}))

    with (
        patch("memento.pi_bridge.get_vault", return_value=tmp_path),
        patch("memento.pi_bridge._is_pid_alive", return_value=True),
    ):
        code = pi_bridge.main(["queue", "process-start", "--project", "repo"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "processing_lock_active"


def test_pi_bridge_queue_list_includes_review_metadata_without_body(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    queue_file = tmp_path / "state" / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir(parents=True)
    body = "First important sentence.\nSecond line with more context.\n" + "x" * 200
    queue_file.write_text(
        json.dumps(
            {
                "id": "q1",
                "created_at": "2026-01-01T00:00:00Z",
                "title": "Queued note",
                "body": body,
                "metadata": {"project": "repo", "branch": "b", "session_id": "s1"},
            }
        )
        + "\n"
    )

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "list"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    capture = payload["captures"][0]
    assert "body" not in capture
    assert capture["body_char_count"] == len(body)
    assert capture["body_size_bytes"] == len(body.encode("utf-8"))
    assert capture["body_kb"] > 0
    assert capture["body_excerpt"].startswith("First important sentence. Second line")


def test_pi_bridge_process_start_repeated_ids_selects_exact_captures(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    queue_file = tmp_path / "state" / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps(
            {
                "id": "q1",
                "created_at": "2026-01-01T00:00:00Z",
                "title": "One",
                "body": "A",
                "metadata": {"project": "repo", "session_id": "s1"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "q2",
                "created_at": "2026-01-01T00:01:00Z",
                "title": "Two",
                "body": "B",
                "metadata": {"project": "repo", "session_id": "s2"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "q3",
                "created_at": "2026-01-01T00:02:00Z",
                "title": "Three",
                "body": "C",
                "metadata": {"project": "other", "session_id": "s3"},
            }
        )
        + "\n"
    )

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "process-start", "--id", "q3", "--id", "q1", "--dry-run"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_capture_count"] == 2
    assert [group["capture_ids"] for group in payload["groups"]] == [["q1"], ["q3"]]


def test_pi_bridge_process_start_limit_applies_to_captures(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    queue_file = tmp_path / "state" / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps(
            {
                "id": "q1",
                "created_at": "2026-01-01T00:00:00Z",
                "title": "One",
                "body": "A",
                "metadata": {"project": "repo", "branch": "b", "session_id": "s1"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "q2",
                "created_at": "2026-01-01T00:01:00Z",
                "title": "Two",
                "body": "B",
                "metadata": {"project": "repo", "branch": "b", "session_id": "s1"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "q3",
                "created_at": "2026-01-01T00:02:00Z",
                "title": "Three",
                "body": "C",
                "metadata": {"project": "repo", "branch": "b", "session_id": "s2"},
            }
        )
        + "\n"
    )

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "process-start", "--project", "repo", "--limit", "1", "--dry-run"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["group_count"] == 1
    assert payload["selected_capture_count"] == 1
    assert payload["groups"][0]["session_id"] == "s1"
    assert payload["groups"][0]["capture_ids"] == ["q1"]


def test_pi_bridge_process_start_releases_lock_on_setup_failure(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    queue_file = tmp_path / "state" / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps(
            {
                "id": "q1",
                "title": "One",
                "body": "A",
                "metadata": {"project": "repo", "branch": "b", "session_id": "s1"},
            }
        )
        + "\n"
    )

    with (
        patch("memento.pi_bridge.get_vault", return_value=tmp_path),
        patch("memento.pi_bridge._render_capture_packet", side_effect=RuntimeError("setup failed")),
    ):
        code = pi_bridge.main(["queue", "process-start", "--project", "repo"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "setup failed"
    assert not (tmp_path / "state" / "processing.lock").exists()


def test_pi_bridge_process_start_includes_small_cleaned_transcript(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    transcript_root = tmp_path / "sessions"
    monkeypatch.setenv("MEMENTO_PI_TRANSCRIPT_ROOTS", str(transcript_root))
    session_file = transcript_root / "session.jsonl"
    transcript_root.mkdir()
    session_file.write_text(
        json.dumps(
            {
                "type": "message",
                "timestamp": "t1",
                "message": {"role": "user", "content": [{"type": "text", "text": "Important decision"}]},
            }
        )
        + "\n"
    )
    queue_file = tmp_path / "state" / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps(
            {
                "id": "q1",
                "title": "One",
                "body": "Body",
                "metadata": {"project": "repo", "branch": "b", "cwd": "/repo", "session_id": str(session_file)},
            }
        )
        + "\n"
    )

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "process-start", "--project", "repo"])
    assert code == 0
    started = json.loads(capsys.readouterr().out)
    run_dir = tmp_path / "state" / "processing" / started["run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    group = manifest["groups"][0]
    assert group["transcript"]["included"] is True
    assert group["cwd"] == "/repo"
    packet = (run_dir / "inputs" / f"{group['group_id']}.md").read_text()
    assert "## Cleaned session transcript" in packet
    assert "Important decision" in packet


def test_pi_bridge_process_start_writes_deterministic_dedup_context(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "redis-cache-ttl.md").write_text(
        "---\n"
        "title: Redis cache requires explicit TTL\n"
        "type: decision\n"
        "tags: [repo, redis, cache]\n"
        "project: repo\n"
        "---\n\nExisting note body.\n"
    )
    (notes / "unrelated.md").write_text("---\ntitle: Unrelated deployment note\ntags: [deploy]\n---\n\nBody.\n")
    queue_file = tmp_path / "state" / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps(
            {
                "id": "q1",
                "title": "Redis cache TTL decision",
                "body": "We decided Redis cache entries need explicit TTLs.",
                "metadata": {"project": "repo", "branch": "b", "cwd": "/repo", "session_id": "s1"},
            }
        )
        + "\n"
    )

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "process-start", "--project", "repo"])
    assert code == 0
    started = json.loads(capsys.readouterr().out)
    run_dir = tmp_path / "state" / "processing" / started["run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    group = manifest["groups"][0]
    assert group["dedup_context"] == [
        {
            "path": "notes/redis-cache-ttl.md",
            "title": "Redis cache requires explicit TTL",
            "type": "decision",
            "tags": ["repo", "redis", "cache"],
            "project": "repo",
        }
    ]
    packet = (run_dir / "inputs" / f"{group['group_id']}.md").read_text()
    assert "## Deduplication context" in packet
    assert "notes/redis-cache-ttl.md: Redis cache requires explicit TTL" in packet
    assert "Unrelated deployment note" not in packet


def test_pi_process_worker_prompt_bans_session_path_boilerplate_in_note_bodies():
    worker = Path("extensions/memento-process-worker.mjs").read_text()
    assert (
        "Preserve the original project/cwd/branch/session metadata from the input packet in captured note bodies"
        not in worker
    )
    assert "Store metadata as memento_capture arguments/frontmatter, never as prose body boilerplate" in worker
    assert "Note bodies must not include labels or raw values for Session ID, CWD, Branch, Capture IDs" in worker
    assert "pass note_type, tags, certainty, cwd, branch, and session_id to memento_capture" in worker


def test_pi_bridge_process_start_skips_transcript_outside_allowed_roots(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    session_file = tmp_path / "outside.jsonl"
    session_file.write_text(
        json.dumps(
            {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "Do not include me"}]}}
        )
        + "\n"
    )
    queue_file = tmp_path / "state" / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps(
            {
                "id": "q1",
                "title": "One",
                "body": "Body",
                "metadata": {"project": "repo", "branch": "b", "cwd": "/repo", "session_id": str(session_file)},
            }
        )
        + "\n"
    )

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "process-start", "--project", "repo"])
    assert code == 0
    started = json.loads(capsys.readouterr().out)
    run_dir = tmp_path / "state" / "processing" / started["run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    group = manifest["groups"][0]
    assert group["transcript"]["included"] is False
    assert group["transcript"]["reason"] == "outside_allowed_roots"
    packet = (run_dir / "inputs" / f"{group['group_id']}.md").read_text()
    assert "Do not include me" not in packet


def test_pi_bridge_process_status_idle_without_runs(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "process-status"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "idle"
    assert payload["active"] is False
    assert payload["groups"] == []


def test_pi_bridge_process_status_reads_progress_file(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    run_dir = tmp_path / "state" / "processing" / "run1"
    run_dir.mkdir(parents=True)
    (run_dir / "progress.json").write_text(
        json.dumps(
            {
                "run_id": "run1",
                "status": "running",
                "created_at": "2026-01-01T00:00:00Z",
                "selected_capture_count": 2,
                "group_count": 2,
                "current_group_id": "g2",
                "groups": [
                    {
                        "group_id": "g1",
                        "status": "processed",
                        "capture_ids": ["q1"],
                        "created": [{"path": "notes/a.md"}],
                    },
                    {"group_id": "g2", "status": "running", "capture_ids": ["q2"]},
                ],
            }
        )
    )
    (tmp_path / "state" / "processing.lock").write_text(
        json.dumps({"run_id": "run1", "pid": 123, "created_time": 9999999999})
    )

    with (
        patch("memento.pi_bridge.get_vault", return_value=tmp_path),
        patch("memento.pi_bridge._is_pid_alive", return_value=True),
    ):
        code = pi_bridge.main(["queue", "process-status"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "running"
    assert payload["active"] is True
    assert payload["completed_group_count"] == 1
    assert payload["pending_group_count"] == 1
    assert payload["current_group_id"] == "g2"


def test_pi_bridge_process_status_marks_stale_progress_interrupted(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    run_dir = tmp_path / "state" / "processing" / "run1"
    run_dir.mkdir(parents=True)
    (run_dir / "progress.json").write_text(
        json.dumps(
            {
                "run_id": "run1",
                "status": "running",
                "selected_capture_count": 1,
                "group_count": 1,
                "groups": [{"group_id": "g1", "status": "running", "capture_ids": ["q1"]}],
            }
        )
    )

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "process-status", "--run-id", "run1"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["active"] is False
    assert payload["status"] == "interrupted"


def test_pi_bridge_process_status_falls_back_to_manifest_results(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    run_dir = tmp_path / "state" / "processing" / "run1"
    results_dir = run_dir / "results"
    logs_dir = run_dir / "logs"
    inputs_dir = run_dir / "inputs"
    results_dir.mkdir(parents=True)
    logs_dir.mkdir()
    inputs_dir.mkdir()
    result_path = results_dir / "g1.json"
    log_path = logs_dir / "g1.md"
    input_path = inputs_dir / "g1.md"
    result_path.write_text(json.dumps({"status": "processed_no_notes", "discard_reason": "noise"}))
    log_path.write_text("log")
    input_path.write_text("input")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "run1",
                "status": "running",
                "created_at": "2026-01-01T00:00:00Z",
                "selected_capture_count": 2,
                "group_count": 2,
                "groups": [
                    {
                        "group_id": "g1",
                        "capture_ids": ["q1"],
                        "input_markdown": str(input_path),
                        "result_json": str(result_path),
                        "log_markdown": str(log_path),
                    },
                    {"group_id": "g2", "capture_ids": ["q2"], "result_json": str(results_dir / "g2.json")},
                ],
            }
        )
    )

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "process-status", "--run-id", "run1"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "interrupted"
    assert payload["completed_group_count"] == 1
    assert payload["groups"][0]["status"] == "processed_no_notes"
    assert payload["groups"][0]["discard_reason"] == "noise"
    assert payload["groups"][1]["status"] == "pending"


def test_pi_bridge_process_finalize_dequeues_no_note_results_with_discard_reason(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    queue_file = tmp_path / "state" / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps(
            {
                "id": "q1",
                "title": "One",
                "body": "Noise",
                "metadata": {"project": "repo", "branch": "b", "session_id": "s1"},
            }
        )
        + "\n"
    )

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "process-start", "--project", "repo"])
    assert code == 0
    started = json.loads(capsys.readouterr().out)
    run_id = started["run_id"]
    run_dir = tmp_path / "state" / "processing" / run_id
    manifest = json.loads((run_dir / "manifest.json").read_text())
    group = manifest["groups"][0]
    (run_dir / "results" / f"{group['group_id']}.json").write_text(
        json.dumps(
            {
                "processed_capture_ids": ["q1"],
                "status": "processed_no_notes",
                "created": [],
                "discard_reason": "No durable content",
            }
        )
    )

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "process-finalize", "--run-id", run_id])
    assert code == 0
    finalized = json.loads(capsys.readouterr().out)
    assert finalized["dequeued"] == 1
    assert queue_file.read_text() == ""


def test_pi_bridge_clean_transcript_drops_thinking_and_caps_tool_results(tmp_path):
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "message",
                "timestamp": "t1",
                "message": {"role": "user", "content": [{"type": "text", "text": "remember the durable decision"}]},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "timestamp": "t2",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "secret reasoning",
                            "thinkingSignature": "sig",
                            "encrypted_content": "blob",
                        },
                        {"type": "toolCall", "name": "memento_capture", "arguments": {"title": "Decision"}},
                    ],
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "timestamp": "t3",
                "message": {"role": "toolResult", "content": [{"type": "toolResult", "text": "x" * 20}]},
            }
        )
        + "\n"
    )

    cleaned = pi_bridge._clean_transcript(session_file, per_tool_cap=5)
    assert "remember the durable decision" in cleaned
    assert "[tool call] memento_capture" in cleaned
    assert "xxxxx\n[tool result truncated]" in cleaned
    assert "secret reasoning" not in cleaned
    assert "thinkingSignature" not in cleaned
    assert "encrypted_content" not in cleaned


def test_pi_bridge_process_finalize_rejects_created_note_paths_outside_vault(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    queue_file = tmp_path / "state" / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps(
            {
                "id": "q1",
                "title": "One",
                "body": "Useful",
                "metadata": {"project": "repo", "branch": "b", "session_id": "s1"},
            }
        )
        + "\n"
    )
    outside = tmp_path / "outside.md"
    outside.write_text("not a vault note")

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path / "vault"):
        (tmp_path / "vault").mkdir()
        code = pi_bridge.main(["queue", "process-start", "--project", "repo"])
    assert code == 0
    started = json.loads(capsys.readouterr().out)
    run_id = started["run_id"]
    run_dir = tmp_path / "state" / "processing" / run_id
    manifest = json.loads((run_dir / "manifest.json").read_text())
    group = manifest["groups"][0]
    (run_dir / "results" / f"{group['group_id']}.json").write_text(
        json.dumps({"processed_capture_ids": ["q1"], "status": "processed", "created": [{"path": str(outside)}]})
    )

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path / "vault"):
        code = pi_bridge.main(["queue", "process-finalize", "--run-id", run_id])
    assert code == 0
    finalized = json.loads(capsys.readouterr().out)
    assert finalized["dequeued"] == 0
    assert json.loads(queue_file.read_text())["id"] == "q1"


def test_pi_bridge_process_finalize_dequeues_only_valid_results(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    queue_file = tmp_path / "state" / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps(
            {
                "id": "q1",
                "title": "One",
                "body": "Useful",
                "metadata": {"project": "repo", "branch": "b", "session_id": "s1"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "q2",
                "title": "Two",
                "body": "Noisy",
                "metadata": {"project": "repo", "branch": "b", "session_id": "s2"},
            }
        )
        + "\n"
    )
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "useful.md").write_text("---\ntitle: Useful\n---\n")

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "process-start", "--project", "repo"])
    assert code == 0
    started = json.loads(capsys.readouterr().out)
    run_id = started["run_id"]
    run_dir = tmp_path / "state" / "processing" / run_id
    manifest = json.loads((run_dir / "manifest.json").read_text())
    by_session = {group["session_id"]: group for group in manifest["groups"]}
    (run_dir / "results" / f"{by_session['s1']['group_id']}.json").write_text(
        json.dumps(
            {
                "processed_capture_ids": ["q1"],
                "status": "processed",
                "created": [{"title": "Useful", "path": "notes/useful.md"}],
            }
        )
    )
    (run_dir / "results" / f"{by_session['s2']['group_id']}.json").write_text(
        json.dumps({"processed_capture_ids": ["q2"], "status": "processed_no_notes", "created": []})
    )

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "process-finalize", "--run-id", run_id])
    assert code == 0
    finalized = json.loads(capsys.readouterr().out)
    assert finalized["dequeued"] == 1
    remaining = [json.loads(line)["id"] for line in queue_file.read_text().splitlines()]
    assert remaining == ["q2"]


def test_pi_bridge_briefing_outputs_error_payload_on_failure(capsys):
    with patch("memento.pi_bridge.build_briefing", side_effect=RuntimeError("boom")):
        code = pi_bridge.main(["briefing", "--cwd", "/repo", "--session-id", "s1"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["should_inject"] is False
    assert payload["source"] == "briefing"
    assert payload["reason"] == "error"
    assert payload["metadata"]["error"] == "boom"
    assert payload["metadata"]["error_type"] == "RuntimeError"


def _seed_cleanup_queue(tmp_path):
    queue_file = tmp_path / "state" / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir(parents=True)
    entries = [
        {
            "id": "raw1",
            "created_at": "2026-05-24T20:00:00Z",
            "title": "Pi session candidate capture",
            "body": '- assistant: [{"type":"thinking","thinking":"secret reasoning"}]',
            "reason": "lifecycle",
            "source_event": "agent_end",
            "metadata": {"project": "repo"},
        },
        {
            "id": "raw2",
            "created_at": "2026-05-24T20:01:00Z",
            "title": "Pi session candidate capture",
            "body": '- toolResult: [{"type":"text","text":"Process preview"}]',
            "reason": "lifecycle",
            "source_event": "session_shutdown",
            "metadata": {"project": "repo"},
        },
        {
            "id": "chatter1",
            "created_at": "2026-05-25T10:00:00Z",
            "title": "Session wrap-up",
            "body": "Looked around the repo and read some files.",
            "reason": "lifecycle",
            "source_event": "agent_end",
            "metadata": {"project": "repo"},
        },
        {
            "id": "durable1",
            "created_at": "2026-05-25T11:00:00Z",
            "title": "Cache bug session",
            "body": "Found the root cause of the cache bug and fixed the TTL handling.",
            "reason": "lifecycle",
            "source_event": "agent_end",
            "metadata": {"project": "repo"},
        },
        {
            "id": "manual1",
            "created_at": "2026-05-25T12:00:00Z",
            "title": "Manually queued decision",
            "body": '- assistant: [{"type":"thinking","thinking":"raw"}]',
            "reason": "manual",
            "source_event": "tool",
            "metadata": {"project": "repo"},
        },
    ]
    queue_file.write_text("".join(json.dumps(e) + "\n" for e in entries) + "not json at all\n")
    return queue_file


def test_pi_bridge_queue_cleanup_dry_run_classifies_without_writing(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    queue_file = _seed_cleanup_queue(tmp_path)
    before = queue_file.read_text()

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "cleanup"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["total"] == 6
    assert payload["by_class"] == {
        "raw_dump": 2,
        "low_value": 1,
        "durable_candidate": 1,
        "manual": 1,
        "invalid": 1,
    }
    # default discard set is conservative: raw dumps + unparseable lines only
    assert payload["discard_classes"] == ["invalid", "raw_dump"]
    assert payload["discarded"] == 3
    assert payload["retained"] == 3
    assert payload["samples"]["raw_dump"][0]["id"] == "raw1"
    assert queue_file.read_text() == before
    assert not list(queue_file.parent.glob("pi-captures-discarded-*.jsonl"))


def test_pi_bridge_queue_cleanup_apply_archives_discarded_with_provenance(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    queue_file = _seed_cleanup_queue(tmp_path)

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(
            [
                "queue",
                "cleanup",
                "--apply",
                "--discard-class",
                "raw_dump",
                "--discard-class",
                "low_value",
                "--discard-class",
                "invalid",
            ]
        )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is False
    assert payload["discarded"] == 4
    assert payload["retained"] == 2

    remaining_ids = [json.loads(line)["id"] for line in queue_file.read_text().splitlines() if line.strip()]
    assert remaining_ids == ["durable1", "manual1"]

    archive = Path(payload["archive_path"])
    assert archive.exists()
    archived = [json.loads(line) for line in archive.read_text().splitlines()]
    assert {entry["id"] for entry in archived if "id" in entry} >= {"raw1", "raw2", "chatter1"}
    for entry in archived:
        assert entry["cleanup"]["class"] in {"raw_dump", "low_value", "invalid"}
        assert entry["cleanup"]["reason"]
        assert entry["cleanup"]["discarded_at"]

    backup = Path(payload["backup_path"])
    assert backup.exists()
    assert "raw1" in backup.read_text()


def test_pi_bridge_queue_cleanup_never_discards_manual_captures(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    queue_file = _seed_cleanup_queue(tmp_path)

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(
            [
                "queue",
                "cleanup",
                "--apply",
                "--discard-class",
                "raw_dump",
                "--discard-class",
                "low_value",
                "--discard-class",
                "invalid",
            ]
        )

    assert code == 0
    remaining_ids = [json.loads(line)["id"] for line in queue_file.read_text().splitlines() if line.strip()]
    # manual1's body looks like a raw dump, but manual captures are preserved
    assert "manual1" in remaining_ids


def test_pi_bridge_queue_cleanup_apply_blocked_during_active_processing_run(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMENTO_PI_STATE_HOME", str(tmp_path / "state"))
    queue_file = _seed_cleanup_queue(tmp_path)
    before = queue_file.read_text()
    lock = tmp_path / "state" / "processing.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "run_id": "active-run"}))

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "cleanup", "--apply"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "processing run active" in payload["blocked"]
    assert payload["dry_run"] is True
    assert queue_file.read_text() == before
