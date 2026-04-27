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


def test_pi_bridge_status_outputs_json(capsys, tmp_path):
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


def test_pi_bridge_search_reports_qmd_unavailable(capsys):
    with (
        patch("memento.pi_bridge.has_qmd", return_value=False),
        patch("memento.pi_bridge.is_remote", return_value=False),
    ):
        code = pi_bridge.main(["search", "--query", "cache"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"results": [], "reason": "qmd-unavailable"}


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


def test_pi_bridge_capture_can_queue_instead_of_write(capsys, tmp_path):
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

    queue_lines = (tmp_path / "queue" / "pi-captures.jsonl").read_text().splitlines()
    queued = json.loads(queue_lines[0])
    assert queued["title"] == "Queued pi capture"
    assert queued["metadata"]["project"] == "repo"
    assert queued["metadata"]["branch"] == "feature/pi"
    assert queued["metadata"]["session_id"] == "s1"


def test_pi_bridge_queue_list_and_flush(capsys, tmp_path):
    queue_file = tmp_path / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir()
    queue_file.write_text(
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

    with patch("memento.pi_bridge.get_vault", return_value=tmp_path):
        code = pi_bridge.main(["queue", "flush", "--id", "q1"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["flushed"] == 1
    assert payload["written"][0]["path"].startswith("notes/queued-pi-capture")
    assert (tmp_path / payload["written"][0]["path"]).exists()
    assert queue_file.read_text() == ""


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
