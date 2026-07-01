import errno
import hashlib
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from memento import health
from memento.config import reset_config


def _make_vault(path: Path):
    for dirname in ("notes", "fleeting", "projects", "archive"):
        (path / dirname).mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    return path


@pytest.fixture(autouse=True)
def isolate_health(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    vault = _make_vault(tmp_path / "vault")
    monkeypatch.setenv("MEMENTO_VAULT_PATH", str(vault))
    monkeypatch.setenv("MEMENTO_SEARCH_BACKEND", "grep")
    monkeypatch.setattr(health, "TRIAGE_HEALTH_LOG_PATH", str(tmp_path / "triage-health.jsonl"))
    monkeypatch.setattr(health, "RETRIEVAL_LOG_PATH", str(tmp_path / "retrieval.jsonl"))
    monkeypatch.setattr(health, "AUTOMATION_MEMORY_HEALTH_LOG_PATH", str(tmp_path / "automation-memory-health.jsonl"))
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


def test_health_deep_flag_runs_opt_in_probes(capsys):
    deep_checks = [health.CheckResult("deep probe", "pass", "ok")]
    with patch.object(health, "_check_deep_diagnostics", return_value=deep_checks) as mock_deep:
        code = health.main(["--json", "--deep"])

    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert mock_deep.called
    assert any(check["name"] == "deep probe" for check in payload["checks"])


def test_health_default_build_report_skips_deep_probes():
    with patch.object(health, "_check_deep_diagnostics") as mock_deep:
        report = health.build_report()

    assert report.status in {"pass", "warn"}
    mock_deep.assert_not_called()


def test_deep_search_probe_uses_selected_backend(monkeypatch):
    calls = []

    class FakeBackend:
        def search(self, query, collection, limit=5, semantic=False, timeout=10, min_score=0.0, concrete=False):
            calls.append(
                {
                    "query": query,
                    "collection": collection,
                    "limit": limit,
                    "semantic": semantic,
                    "timeout": timeout,
                    "min_score": min_score,
                    "concrete": concrete,
                }
            )
            return [{"path": "notes/probe.md", "title": "Probe", "score": 1.0, "snippet": "probe"}]

    monkeypatch.setattr(health, "reset_backend", lambda: None)
    monkeypatch.setattr(health, "get_backend", lambda: FakeBackend())

    result = health._check_deep_search_probe(config={"qmd_collection": "memento"}, probe_timeout_seconds=7)

    assert result.status == "pass"
    assert result.details["timeout_seconds"] == 7
    assert result.details["result_count"] == 1
    assert calls == [
        {
            "query": "memento-vault health probe",
            "collection": "memento",
            "limit": 1,
            "semantic": False,
            "timeout": 7,
            "min_score": 0.0,
            "concrete": True,
        }
    ]


def test_deep_mcp_probe_calls_tools(monkeypatch):
    calls = []
    import memento.mcp_server as mcp_server

    monkeypatch.setattr(mcp_server, "memento_status", lambda: {"vault_exists": True, "qmd_available": True})
    monkeypatch.setattr(
        mcp_server,
        "memento_search",
        lambda *args, **kwargs: calls.append((args, kwargs)) or [{"path": "notes/probe.md"}],
    )

    result = health._check_deep_mcp_probe(vault=Path("/tmp/vault"), probe_timeout_seconds=5)

    assert result.status == "pass"
    assert result.details["search_result_count"] == 1
    assert calls == [
        (("memento-vault health probe",), {"limit": 1, "semantic": False, "min_score": 0.0, "cwd": "/tmp/vault"})
    ]


def test_deep_pi_bridge_probe_uses_short_timeout(monkeypatch):
    run_calls = []

    def fake_run(*args, **kwargs):
        run_calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "ok"}), stderr="")

    monkeypatch.setattr(health.subprocess, "run", fake_run)

    result = health._check_deep_pi_bridge_probe(vault=Path("/tmp/vault"), probe_timeout_seconds=4)

    assert result.status == "pass"
    assert run_calls[0]["timeout"] == 4
    assert run_calls[0]["cwd"] == str(health._repo_root())


def test_deep_remote_probe_uses_short_timeout(monkeypatch):
    calls = []

    monkeypatch.setattr(
        health.remote_client, "status", lambda timeout=30: calls.append(("status", timeout)) or {"vault_exists": True}
    )
    monkeypatch.setattr(
        health.remote_client,
        "search_envelope",
        lambda query, limit=5, semantic=False, min_score=0.0, cwd="", concrete="auto", timeout=30: (
            calls.append(("search", query, timeout)) or {"results": []}
        ),
    )

    result = health._check_deep_remote_probe(probe_timeout_seconds=3)

    assert result.status == "pass"
    assert calls == [("status", 3), ("search", "memento-vault health probe", 3)]


def test_health_json_exposes_automation_memory_readiness(capsys):
    Path(health.AUTOMATION_MEMORY_HEALTH_LOG_PATH).write_text(
        json.dumps(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "hook": "automation-memory",
                "action": "packet_success",
                "source": "session-context",
                "should_inject": True,
                "result_count": 2,
            }
        )
        + "\n"
    )

    code = health.main(["--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    automation = payload["automation_memory"]
    assert automation["metadata"]["cheap_read_only"] is True
    assert automation["metadata"]["network_checked"] is False
    assert automation["metadata"]["search"]["available"] is True
    assert automation["metadata"]["last_successful_packet"]["source"] == "session-context"
    assert automation["metadata"]["probe"]["name"] == "automation_memory"


def test_automation_memory_reports_recall_failure_rate_and_reasons():
    token = "sk-" + "a" * 24
    Path(health.RETRIEVAL_LOG_PATH).write_text(
        "\n".join(
            [
                json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "hook": "recall", "action": "inject"}),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "recall",
                        "action": "backend_unavailable",
                        "error": f"auth failed {token}",
                    }
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "search",
                        "action": "unexpected_error",
                        "error": "qmd timeout",
                    }
                ),
            ]
        )
        + "\n"
    )

    readiness = health.build_automation_memory_readiness()

    assert readiness["status"] == "warn"
    recall = readiness["metadata"]["recall"]
    assert recall["events"] == 3
    assert recall["failures"] == 2
    assert recall["failure_rate"] == 0.6667
    assert recall["backend_unavailable"] == 1
    assert recall["backend_exceptions"] == 1
    assert token not in json.dumps(readiness)
    reasons = readiness["metadata"]["common_failure_reasons"]
    assert any(reason["reason"] == "auth failed [REDACTED_API_KEY]" for reason in reasons)


def test_health_recent_jsonl_normalizes_offset_aware_timestamps(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-06-30T11:59:00", "action": "old"}),
                json.dumps({"ts": "2026-06-30T12:00:00Z", "action": "zulu"}),
                json.dumps({"ts": "2026-06-30T08:01:00-04:00", "action": "offset"}),
            ]
        )
        + "\n"
    )

    cutoff = datetime(2026, 6, 30, 12, 0)

    assert [rec["action"] for rec in health._iter_recent_jsonl(path, cutoff)] == ["zulu", "offset"]


def test_retrieval_health_distinguishes_failures_misses_and_low_signal_skips():
    Path(health.RETRIEVAL_LOG_PATH).write_text(
        "\n".join(
            [
                json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "hook": "recall", "action": "inject"}),
                json.dumps(
                    {"ts": datetime.now().isoformat(timespec="seconds"), "hook": "recall", "action": "no-results"}
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "mcp",
                        "action": "search_miss",
                        "reason": "backend_unavailable",
                        "error": "qmd missing",
                    }
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "search",
                        "action": "qmd_search_unexpected",
                        "error": "sqlite locked",
                    }
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "recall",
                        "action": "low-signal-prompt",
                    }
                ),
            ]
        )
        + "\n"
    )

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "retrieval")

    assert check.status == "warn"
    assert check.details["events"] == 4
    assert check.details["failures"] == 2
    assert check.details["no_results"] == 1
    assert check.details["backend_unavailable"] == 1
    assert check.details["backend_exceptions"] == 1
    assert check.details["low_signal_skips"] == 1
    assert check.details["last_error"] == "sqlite locked"
    assert "backend failures 2/4" in check.message
    assert check.details["remediation"]


def test_retrieval_health_keeps_no_results_out_of_failure_rate():
    Path(health.RETRIEVAL_LOG_PATH).write_text(
        "\n".join(
            [
                json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "hook": "mcp", "action": "search"}),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "mcp",
                        "action": "search_miss",
                        "reason": "no_exact_match",
                    }
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "recall",
                        "action": "low-signal-prompt",
                    }
                ),
            ]
        )
        + "\n"
    )

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "retrieval")

    assert check.status == "pass"
    assert check.details["events"] == 2
    assert check.details["failures"] == 0
    assert check.details["no_results"] == 1
    assert check.details["low_signal_skips"] == 1


def test_retrieval_last_error_is_redacted_and_truncated():
    token = "ghp_" + "a" * 36
    long_error = f"boom {token} " + "x" * 900
    Path(health.RETRIEVAL_LOG_PATH).write_text(
        json.dumps(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "hook": "search",
                "action": "qmd_search_unexpected",
                "error": long_error,
            }
        )
        + "\n"
    )

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "retrieval")

    assert token not in json.dumps(check.details)
    assert "[REDACTED_GITHUB_TOKEN]" in check.details["last_error"]
    assert check.details["last_error_truncated"] is True
    assert len(check.details["last_error"]) <= 503


def test_automation_memory_reports_stale_embedded_index(tmp_path):
    vault = _make_vault(tmp_path / "stale-vault")
    db = vault / ".search" / "search.db"
    db.parent.mkdir()
    db.write_text("index")
    note = vault / "notes" / "newer.md"
    note.write_text("newer")
    old = (datetime.now() - timedelta(hours=2)).timestamp()
    now = datetime.now().timestamp()
    os.utime(db, (old, old))
    os.utime(note, (now, now))

    readiness = health.build_automation_memory_readiness(
        config={"vault_path": str(vault), "search_backend": "embedded", "search_db_path": ".search/search.db"},
        vault=vault,
    )

    assert readiness["status"] == "warn"
    stale = readiness["metadata"]["search"]["stale_index"]
    assert stale["stale"] is True
    assert stale["lag_seconds"] > 60


def test_automation_memory_reports_remote_sync_pending_retries(tmp_path, monkeypatch):
    vault = _make_vault(tmp_path / "remote-vault")
    monkeypatch.setenv("MEMENTO_VAULT_URL", "https://vault.example.com/mcp")
    ledger = vault / ".sync" / "ledger.jsonl"
    ledger.parent.mkdir()
    ledger.write_text(
        "\n".join(
            [
                json.dumps(
                    {"ts": datetime.now().isoformat(timespec="seconds"), "kind": "note", "source": "a", "status": "ok"}
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "kind": "capture",
                        "source": "session:s1",
                        "status": "error",
                        "error": "remote unavailable",
                    }
                ),
            ]
        )
        + "\n"
    )

    readiness = health.build_automation_memory_readiness(
        config={"vault_path": str(vault), "search_backend": "grep", "search_db_path": ".search/search.db"},
        vault=vault,
    )

    assert readiness["status"] == "warn"
    remote = readiness["metadata"]["remote_sync"]
    assert remote["remote_configured"] is True
    assert remote["network_checked"] is False
    assert remote["pending_retry_count"] == 1
    assert remote["pending_kinds"] == ["capture"]


def test_health_reports_local_extraction_retry_backlog_and_dead_letters(tmp_path):
    vault = _make_vault(tmp_path / "local-retry-vault")
    ledger = vault / ".sync" / "ledger.jsonl"
    ledger.parent.mkdir()
    ledger.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "kind": "local-extraction",
                        "source": "session:s1",
                        "status": "error",
                        "error": "llm timed out",
                    }
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "kind": "local-extraction",
                        "source": "session:s2",
                        "status": "dead-letter",
                        "error": "attempts exhausted",
                    }
                ),
            ]
        )
        + "\n"
    )

    retry_check = health._check_local_extraction_retries(vault)
    readiness = health.build_automation_memory_readiness(
        config={"vault_path": str(vault), "search_backend": "grep", "search_db_path": ".search/search.db"},
        vault=vault,
    )

    assert retry_check.status == "warn"
    assert retry_check.details["pending_retry_count"] == 1
    assert retry_check.details["dead_letter_count"] == 1
    local = readiness["metadata"]["local_extraction_retries"]
    assert readiness["status"] == "warn"
    assert local["pending_retry_count"] == 1
    assert local["dead_letter_count"] == 1


def test_inception_health_warns_when_state_missing(monkeypatch):
    monkeypatch.setattr(health, "_missing_inception_dependencies", lambda: [])

    report = health._check_inception({"inception_enabled": True})

    assert report.status == "warn"
    assert report.details["state_present"] is False
    assert "state file is missing" in report.message


def test_inception_health_fails_on_missing_optional_dependencies(monkeypatch):
    monkeypatch.setattr(health, "_missing_inception_dependencies", lambda: ["numpy", "hdbscan"])
    Path(health.INCEPTION_STATE_PATH).write_text(
        json.dumps({"last_run_iso": datetime.now().isoformat(timespec="seconds"), "last_run_note_count": 1, "runs": []})
    )

    report = health._check_inception({"inception_enabled": True})

    assert report.status == "fail"
    assert report.details["missing_dependencies"] == ["numpy", "hdbscan"]
    assert "missing optional dependencies" in report.message


def test_inception_health_surfaces_recent_run_summary_and_error(monkeypatch):
    monkeypatch.setattr(health, "_missing_inception_dependencies", lambda: [])
    token = "ghp_" + "a" * 36
    long_error = f"boom {token} " + "x" * 650
    now = datetime.now()
    state = {
        "last_run_iso": (now - timedelta(hours=2, minutes=5)).isoformat(timespec="seconds"),
        "last_run_note_count": 7,
        "runs": [
            {
                "iso": (now - timedelta(hours=3)).isoformat(timespec="seconds"),
                "clusters_found": 1,
                "notes_written": 0,
                "dry_run": True,
            },
            {
                "iso": (now - timedelta(hours=2, minutes=5)).isoformat(timespec="seconds"),
                "clusters_found": 4,
                "notes_written": 2,
                "dry_run": False,
                "error": long_error,
            },
        ],
        "processed_notes": ["a", "b"],
    }
    Path(health.INCEPTION_STATE_PATH).write_text(json.dumps(state))

    report = health._check_inception({"inception_enabled": True})
    payload = json.dumps(report.details)

    assert report.status == "pass"
    assert report.details["state_valid"] is True
    assert report.details["run_count"] == 2
    assert report.details["processed_notes_count"] == 2
    assert report.details["last_run_note_count"] == 7
    assert len(report.details["recent_runs"]) == 2
    assert report.details["last_error_truncated"] is True
    assert "[REDACTED_GITHUB_TOKEN]" in report.details["last_error"]
    assert token not in payload
    assert "last ran" in report.message
    assert "last run note count 7" in report.message


def test_inception_health_reports_live_lock(monkeypatch):
    monkeypatch.setattr(health, "_missing_inception_dependencies", lambda: [])
    Path(health.INCEPTION_STATE_PATH).write_text(
        json.dumps({"last_run_iso": datetime.now().isoformat(timespec="seconds"), "last_run_note_count": 1, "runs": []})
    )
    lock = Path(health.INCEPTION_LOCK_PATH)
    lock.write_text(str(os.getpid()))
    stale = time.time() - 700
    os.utime(lock, (stale, stale))

    report = health._check_inception({"inception_enabled": True})

    assert report.status == "fail"
    assert report.details["lock"]["status"] == "fail"
    assert "long-running live pid" in report.details["lock"]["message"]


def test_health_prefers_xdg_config_dir_when_file_exists(tmp_path):
    home_config = Path.home() / ".config" / "memento-vault"
    xdg_config = Path(os.environ["XDG_CONFIG_HOME"]) / "memento-vault"
    home_config.mkdir(parents=True)
    xdg_config.mkdir(parents=True)
    (home_config / "manifest.json").write_text(json.dumps({"version": "home", "files": {}}))
    (xdg_config / "manifest.json").write_text(json.dumps({"version": "xdg", "files": {}}))

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "install manifest")

    assert check.details["path"] == str(xdg_config / "manifest.json")
    assert check.details["version"] == "xdg"


def test_health_falls_back_to_installer_config_dir_when_xdg_file_missing(tmp_path):
    home_config = Path.home() / ".config" / "memento-vault"
    home_config.mkdir(parents=True)
    (home_config / "manifest.json").write_text(json.dumps({"version": "home", "files": {}}))

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "install manifest")

    assert check.details["path"] == str(home_config / "manifest.json")
    assert check.details["version"] == "home"


def test_install_manifest_health_reports_present_manifest(tmp_path):
    config_dir = health._config_dir()
    config_dir.mkdir(parents=True)
    manifest = config_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "4.1.0",
                "vault_path": str(tmp_path / "vault"),
                "options": {"experimental": True, "mcp": True},
                "files": {"hooks/memento-triage.py": "abc123"},
            }
        )
    )

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "install manifest")

    assert check.status == "pass"
    assert "4.1.0" in check.message
    assert check.details["file_count"] == 1
    assert check.details["options"] == ["experimental", "mcp"]
    assert "vault_path" not in check.details


def test_install_manifest_health_warns_when_missing():
    report = health.build_report()
    check = next(check for check in report.checks if check.name == "install manifest")

    assert check.status == "warn"
    assert "./install.sh --reinstall" in check.message


def test_health_warns_on_non_utf8_manifest_settings_and_pi_config():
    config_dir = health._config_dir()
    config_dir.mkdir(parents=True)
    (config_dir / "manifest.json").write_bytes(b"\xff")
    (config_dir / "pi-bridge.json").write_bytes(b"\xff")
    settings = Path.home() / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b"\xff")

    report = health.build_report()
    checks = {check.name: check for check in report.checks}

    assert checks["install manifest"].status == "warn"
    assert checks["claude hooks"].status == "warn"
    assert checks["pi bridge"].status == "warn"


def test_managed_file_drift_reports_stale_local_and_missing_critical():
    def sha(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    claude_hooks = Path.home() / ".claude" / "hooks"
    claude_hooks.mkdir(parents=True)
    (claude_hooks / "memento-triage.py").write_text("old managed copy")
    (claude_hooks / "vault-commit.sh").write_text("local edited hook")

    config_dir = health._config_dir()
    config_dir.mkdir(parents=True)
    (config_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": "4.1.0",
                "files": {
                    "hooks/memento-triage.py": sha("old managed copy"),
                    "hooks/vault-commit.sh": sha("previous managed copy"),
                    "memento/config.py": sha("previous package copy"),
                },
            }
        )
    )

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "managed files")

    assert check.status == "fail"
    assert "./install.sh --reinstall" in check.message
    assert "hooks/memento-triage.py" in check.details["stale_managed"]
    assert "hooks/vault-commit.sh" in check.details["locally_modified"]
    assert "memento/config.py" in check.details["missing_critical"]


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


def test_claude_hook_registration_warns_when_retrieval_hooks_missing():
    config_dir = health._config_dir()
    config_dir.mkdir(parents=True)
    (config_dir / "manifest.json").write_text(json.dumps({"version": "4.1.0", "options": {}, "files": {}}))
    settings = Path.home() / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "python3 /tmp/memento-triage.py"}]}]}}
        )
    )

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "claude hooks")

    assert check.status == "warn"
    assert "./install.sh --reinstall" in check.message
    assert "SessionStart/vault-briefing.py" in check.details["missing"]
    assert "UserPromptSubmit/vault-recall.py" in check.details["missing"]
    assert "PreToolUse/vault-tool-context.py" in check.details["missing"]


def test_mcp_remote_shape_valid_and_redacts_headers(capsys):
    token = "sk-" + "a" * 24
    config_path = Path.home() / ".claude" / "mcp-servers.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "memento-vault": {
                    "type": "http",
                    "url": "https://vault.example.com/mcp",
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        )
    )

    code = health.main(["--json"])
    payload = json.loads(capsys.readouterr().out)
    check = next(check for check in payload["checks"] if check["name"] == "mcp config")

    assert code == 0
    assert check["status"] == "pass"
    assert check["details"]["memento_vault"] == "remote http"
    assert token not in json.dumps(payload)


def test_mcp_registration_warns_when_cli_registration_missing(monkeypatch):
    config_path = Path.home() / ".claude" / "mcp-servers.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"memento-vault": {"type": "http", "url": "https://vault.example.com/mcp"}}))

    def fake_which(binary):
        return f"/usr/bin/{binary}" if binary == "claude" else None

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="not found")

    monkeypatch.setattr(health.shutil, "which", fake_which)
    monkeypatch.setattr(health.subprocess, "run", fake_run)

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "mcp registration")

    assert check.status == "warn"
    assert "./install.sh --mcp" in check.message
    assert check.details["registrations"][0]["client"] == "claude"
    assert check.details["registrations"][0]["status"] == "warn"
    assert check.details["registrations"][0]["reason"] == "not found"


def test_mcp_registration_error_reason_is_redacted(monkeypatch):
    token = "ghp_" + "a" * 36

    def fake_which(binary):
        return f"/usr/bin/{binary}" if binary == "claude" else None

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr=f"auth failed {token}")

    monkeypatch.setattr(health.shutil, "which", fake_which)
    monkeypatch.setattr(health.subprocess, "run", fake_run)

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "mcp registration")

    assert check.status == "warn"
    assert check.details["registrations"][0]["reason"] == "auth failed [REDACTED_GITHUB_TOKEN]"


def test_mcp_registration_passes_when_cli_registration_exists(monkeypatch):
    def fake_which(binary):
        return f"/usr/bin/{binary}" if binary == "codex" else None

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="memento-vault: http https://vault.example.com/mcp", stderr="")

    monkeypatch.setattr(health.shutil, "which", fake_which)
    monkeypatch.setattr(health.subprocess, "run", fake_run)

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "mcp registration")

    assert check.status == "pass"
    assert {"client": "codex", "status": "pass", "shape": "remote http"} in check.details["registrations"]


def test_mcp_local_stdio_shape_valid():
    config_path = Path.home() / ".claude" / "mcp-servers.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "memento-vault": {
                    "command": "python3",
                    "args": ["-m", "memento"],
                    "env": {"PYTHONPATH": str(Path.home() / ".claude" / "hooks")},
                }
            }
        )
    )

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "mcp config")

    assert check.status == "pass"
    assert check.details["memento_vault"] == "local stdio"


def test_mcp_registration_shape_rejects_wrong_python_module():
    assert health._mcp_registration_shape("memento-vault: stdio python3 -m other_module") == "invalid"
    assert health._mcp_registration_shape("memento-vault: stdio python3 /tmp/server.py") == "invalid"
    assert health._mcp_registration_shape("memento-vault: stdio python3 -m memento") == "local stdio"
    assert health._mcp_registration_shape("memento-vault\n  Command: python3\n  Args: -m memento") == "local stdio"
    assert health._mcp_registration_shape("memento-vault: http https://vault.example.com") == "invalid"
    assert health._mcp_registration_shape("memento-vault: http https://vault.example.com/mcp") == "remote http"


def test_mcp_local_stdio_shape_rejects_reordered_args():
    assert (
        health._mcp_entry_shape(
            {
                "memento-vault": {
                    "command": "python3",
                    "args": ["memento", "-m"],
                    "env": {"PYTHONPATH": "/tmp/hooks"},
                }
            }
        )[0]
        == "invalid"
    )


def test_invalid_pi_bridge_config_warns():
    config_path = health._config_dir() / "pi-bridge.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"piBridge": {"enabled": "yes", "maxInjectedChars": -1}}))

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "pi bridge")

    assert check.status == "warn"
    assert "enabled" in check.details["invalid_keys"]
    assert "maxInjectedChars" in check.details["invalid_keys"]


def test_recent_pi_bridge_failures_warn():
    Path(health.TRIAGE_HEALTH_LOG_PATH).write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "pi-bridge",
                        "action": "briefing_failed",
                        "operation": "briefing",
                        "backend": "python3",
                        "cwd": "/repo",
                        "project": "repo",
                        "session_id": "s1",
                        "error": "python3: command not found",
                    }
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "pi-bridge",
                        "action": "capture_failed",
                        "operation": "capture",
                        "backend": "python3",
                        "cwd": "/repo",
                        "project": "repo",
                        "session_id": "s1",
                        "error": "stdout parse failed",
                    }
                ),
            ]
        )
        + "\n"
    )

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "pi bridge health")

    assert check.status == "warn"
    assert check.details["events"] == 2
    assert check.details["failures"] == 2
    assert check.details["last_failure"]["operation"] == "capture"
    assert "Pi bridge failures 2" in check.message
    assert "stdout parse failed" in check.details["last_failure"]["error"]


def test_recent_pi_bridge_success_records_do_not_warn():
    Path(health.TRIAGE_HEALTH_LOG_PATH).write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds") + "Z",
                        "hook": "pi-bridge",
                        "action": "triage_spawned",
                    }
                ),
                json.dumps(
                    {"ts": datetime.now().isoformat(timespec="seconds"), "hook": "pi-bridge", "action": "pi_decision"}
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "pi-bridge",
                        "action": "pi_structured_notes_written",
                    }
                ),
            ]
        )
        + "\n"
    )

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "pi bridge health")

    assert check.status == "pass"
    assert "no recent Pi bridge failures" in check.message


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


def test_recent_certainty_string_triage_fail_points_to_reinstall():
    certainty_error = "invalid literal for int() with base 10: 'confirmed'"
    Path(health.TRIAGE_HEALTH_LOG_PATH).write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "triage",
                        "action": "structured_notes_failed",
                        "error": certainty_error,
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
                        "action": "structured_notes_failed",
                    }
                ),
            ]
        )
        + "\n"
    )

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "triage")

    assert check.status == "warn"
    assert "stale installed memento package" in check.message
    assert "./install.sh --reinstall" in check.message
    assert "certainty labels like confirmed" in check.message
    assert check.details["last_error"] == certainty_error


def test_recent_mixed_mcp_and_certainty_triage_fail_reports_both_hints():
    invalid_mcp_error = (
        "Error: Invalid MCP configuration:\nmcpServers: Does not adhere to MCP server configuration schema"
    )
    certainty_error = "invalid literal for int() with base 10: 'confirmed'"
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
                        "action": "structured_notes_failed",
                        "error": certainty_error,
                    }
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "triage",
                        "action": "structured_notes_parse_empty",
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
    assert "stale installed memento package" in check.message
    assert "certainty labels like confirmed" in check.message
    assert report.status == "fail"


def test_recent_certainty_string_triage_fail_detects_other_accepted_labels():
    certainty_error = "invalid literal for int() with base 10: 'verified'"
    Path(health.TRIAGE_HEALTH_LOG_PATH).write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "triage",
                        "action": "structured_notes_failed",
                        "error": certainty_error,
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
                        "action": "structured_notes_failed",
                    }
                ),
            ]
        )
        + "\n"
    )

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "triage")

    assert check.status == "warn"
    assert "stale installed memento package" in check.message
    assert "./install.sh --reinstall" in check.message


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

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "config")

    assert check.status == "fail"
    assert "cannot parse" in check.message


def test_simple_yaml_parser_handles_empty_and_spaced_lists(tmp_path):
    config = tmp_path / "memento.yml"
    config.write_text("extra_qmd_collections: []\nrecall_skip_patterns: [ \"a\", 'b' ]\n")

    parsed = health._parse_simple_yaml(config)

    assert parsed["extra_qmd_collections"] == []
    assert parsed["recall_skip_patterns"] == ["a", "b"]


def test_explicit_qmd_backend_missing_is_fail(monkeypatch):
    monkeypatch.setenv("MEMENTO_SEARCH_BACKEND", "qmd")

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


def test_health_subprocess_does_not_create_runtime_or_cache_dirs(tmp_path):
    vault = _make_vault(tmp_path / "vault")
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["MEMENTO_VAULT_PATH"] = str(vault)
    env["MEMENTO_SEARCH_BACKEND"] = "grep"

    result = subprocess.run(
        [sys.executable, "-m", "memento.health", "--json"],
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] in {"pass", "warn"}
    assert not (home / ".cache" / "memento-vault").exists()
    assert not (runtime / "memento-vault").exists()


def test_unreadable_mcp_config_reports_failure():
    config_path = Path.home() / ".claude" / "mcp-servers.json"
    config_path.mkdir(parents=True)

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "mcp config")

    assert check.status == "fail"
    assert "cannot read MCP config" in check.message


def test_human_report_does_not_truncate_away_later_checks():
    checks = [
        health.CheckResult("first", "warn", "x" * 1200),
        health.CheckResult("last-check", "pass", "still visible"),
    ]
    report = health.HealthReport(status="warn", summary={"pass": 1, "warn": 1, "fail": 0}, checks=checks)

    rendered = health.render_human(report)

    assert "last-check" in rendered
    assert "still visible" in rendered


def test_single_invalid_mcp_failure_below_threshold_does_not_fail():
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
                        "action": "structured_notes_written",
                    }
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "triage",
                        "action": "structured_notes_written",
                    }
                ),
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "hook": "triage",
                        "action": "structured_notes_written",
                    }
                ),
            ]
        )
        + "\n"
    )

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "triage")

    assert check.status == "pass"
    assert report.status != "fail"


def test_lock_check_inspects_temp_fallback_path(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    lock_dir = tmp_path / f"memento-vault-{os.getuid()}"
    lock_dir.mkdir()
    lock = lock_dir / "vault-write.lock"
    lock.write_text(str(os.getpid()))
    old = time.time() - 700
    os.utime(lock, (old, old))

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "locks")

    assert check.status == "fail"
    assert str(lock) in json.dumps(check.details)


def test_pid_is_live_treats_permission_error_as_live(monkeypatch):
    def deny_signal(pid, sig):
        raise PermissionError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(health.os, "kill", deny_signal)

    assert health._pid_is_live(12345) is True


def test_pid_is_live_returns_false_for_missing_process(monkeypatch):
    def missing_process(pid, sig):
        raise OSError(errno.ESRCH, "no such process")

    monkeypatch.setattr(health.os, "kill", missing_process)

    assert health._pid_is_live(12345) is False
