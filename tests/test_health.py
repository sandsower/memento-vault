import errno
import hashlib
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path

import pytest

from memento import health


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
    monkeypatch.setattr(health, "INCEPTION_STATE_PATH", str(tmp_path / "inception-state.json"))
    monkeypatch.setattr(health, "VAULT_WRITE_LOCK_PATH", str(tmp_path / "vault-write.lock"))
    monkeypatch.setattr(health, "INCEPTION_LOCK_PATH", str(tmp_path / "inception.lock"))
    yield


def test_health_json_outputs_report_and_default_allows_warnings(capsys):
    code = health.main(["--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] in {"pass", "warn"}
    assert "checks" in payload
    assert any(check["name"] == "vault" for check in payload["checks"])


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


def test_claude_hook_registration_warns_when_expected_hook_missing():
    config_dir = health._config_dir()
    config_dir.mkdir(parents=True)
    (config_dir / "manifest.json").write_text(json.dumps({"version": "4.1.0", "options": {"experimental": True}, "files": {}}))
    settings = Path.home() / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"SessionEnd": []}}))

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "claude hooks")

    assert check.status == "warn"
    assert "./install.sh --reinstall" in check.message
    assert "SessionEnd/memento-triage.py" in check.details["missing"]
    assert "SessionStart/vault-briefing.py" in check.details["missing"]


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
    assert health._mcp_entry_shape(
        {
            "memento-vault": {
                "command": "python3",
                "args": ["memento", "-m"],
                "env": {"PYTHONPATH": "/tmp/hooks"},
            }
        }
    )[0] == "invalid"


def test_invalid_pi_bridge_config_warns():
    config_path = health._config_dir() / "pi-bridge.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"piBridge": {"enabled": "yes", "maxInjectedChars": -1}}))

    report = health.build_report()
    check = next(check for check in report.checks if check.name == "pi bridge")

    assert check.status == "warn"
    assert "enabled" in check.details["invalid_keys"]
    assert "maxInjectedChars" in check.details["invalid_keys"]


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
