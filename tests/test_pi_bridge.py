import json
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
    assert (tmp_path / payload["path"]).exists()


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
