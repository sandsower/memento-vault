import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from memento import health
from memento.config import reset_config


def _make_vault(path: Path):
    for dirname in ("notes", "fleeting", "projects", "archive"):
        (path / dirname).mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


@pytest.fixture(autouse=True)
def isolate_health(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    vault = _make_vault(tmp_path / "vault")
    monkeypatch.setenv("MEMENTO_VAULT_PATH", str(vault))
    monkeypatch.setenv("MEMENTO_SEARCH_BACKEND", "grep")
    monkeypatch.setattr(health, "TRIAGE_HEALTH_LOG_PATH", str(tmp_path / "triage-health.jsonl"))
    monkeypatch.setattr(health, "RETRIEVAL_LOG_PATH", str(tmp_path / "retrieval.jsonl"))
    monkeypatch.setattr(health, "INCEPTION_STATE_PATH", str(tmp_path / "inception-state.json"))
    monkeypatch.setattr(health, "VAULT_WRITE_LOCK_PATH", str(tmp_path / "vault-write.lock"))
    monkeypatch.setattr(health, "INCEPTION_LOCK_PATH", str(tmp_path / "inception.lock"))
    reset_config()
    yield
    reset_config()


def test_health_json_outputs_report_and_default_allows_warnings(capsys):
    code = health.main(["--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] in {"pass", "warn"}
    assert "checks" in payload
    assert any(check["name"] == "vault" for check in payload["checks"])


def test_strict_exits_nonzero_on_warnings():
    report = health.HealthReport(
        status="warn",
        summary={"pass": 0, "warn": 1, "fail": 0},
        checks=[health.CheckResult("example", "warn", "warning")],
    )

    assert health.exit_code(report, strict=False) == 0
    assert health.exit_code(report, strict=True) == 1


def test_failures_always_exit_nonzero():
    report = health.HealthReport(
        status="fail",
        summary={"pass": 0, "warn": 0, "fail": 1},
        checks=[health.CheckResult("example", "fail", "broken")],
    )

    assert health.exit_code(report, strict=False) == 1
    assert health.exit_code(report, strict=True) == 1


def test_stale_headless_mcp_config_static_check_warns(tmp_path):
    stale = Path.home() / ".claude" / "hooks" / "memento" / "llm.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("cmd = [\n    'claude',\n    '--strict-mcp-config',\n    '--mcp-config',\n    '{}',\n]\n")

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "headless claude mcp")

    assert check.status == "warn"
    assert "./install.sh --reinstall" in check.message
    assert str(stale) in check.details["paths"]


def test_recent_invalid_mcp_triage_fail_escalates():
    invalid_mcp_error = (
        "Error: Invalid MCP configuration:\nmcpServers: Does not adhere to MCP server configuration schema"
    )
    Path(health.TRIAGE_HEALTH_LOG_PATH).write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "triage",
                        "action": "structured_notes_llm_failed",
                        "error": invalid_mcp_error,
                    }
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "triage",
                        "action": "structured_notes_parse_empty",
                    }
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "triage",
                        "action": "structured_notes_llm_failed",
                    }
                ),
            ]
        )
        + "\n"
    )

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "triage")

    assert check.status == "fail"
    assert "stale headless Claude MCP config" in check.message
    assert report.status == "fail"


def test_output_redacts_secrets_in_verbose_and_json(capsys):
    token = "ghp_" + "a" * 36
    Path(health.TRIAGE_HEALTH_LOG_PATH).write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "triage",
                        "action": "structured_notes_llm_failed",
                        "error": f"backend failed with token {token}",
                    }
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "triage",
                        "action": "structured_notes_parse_empty",
                    }
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "triage",
                        "action": "structured_notes_llm_failed",
                    }
                ),
            ]
        )
        + "\n"
    )

    report = health.build_report()
    human = health.render_human(report, verbose=True)
    payload = json.dumps(report.to_dict(verbose=True))

    assert token not in human
    assert token not in payload
    assert "[REDACTED_GITHUB_TOKEN]" in human


def test_config_parse_failure_is_fail(tmp_path):
    config_dir = Path.home() / ".config" / "memento-vault"
    config_dir.mkdir(parents=True)
    (config_dir / "memento.yml").write_text("vault_path: [unterminated\n")
    reset_config()

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "config")

    assert check.status == "fail"
    assert "cannot parse" in check.message


def test_explicit_qmd_backend_missing_is_fail(monkeypatch):
    monkeypatch.setenv("MEMENTO_SEARCH_BACKEND", "qmd")
    reset_config()

    real_which = health.shutil.which

    def fake_which(binary):
        if binary == "qmd":
            return None
        return real_which(binary)

    monkeypatch.setattr(health.shutil, "which", fake_which)

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "search")

    assert check.status == "fail"
    assert "qmd is not on PATH" in check.message


def test_stale_live_lock_is_fail(monkeypatch):
    lock = Path(health.VAULT_WRITE_LOCK_PATH)
    lock.write_text(str(os.getpid()))
    old = time.time() - 700
    os.utime(lock, (old, old))

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "locks")

    assert check.status == "fail"
    assert "long-running live pid" in check.message
