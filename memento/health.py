"""Read-only operational health diagnostics for memento-vault."""

from __future__ import annotations

import argparse
import errno
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from memento import remote_client, telemetry
from memento.search_backend import get_backend, reset_backend


PASS = "pass"
WARN = "warn"
FAIL = "fail"
_STATUSES = (PASS, WARN, FAIL)
_EXPECTED_DIRS = ("notes", "fleeting", "projects", "archive")
_CORE_DIRS = ("notes", "fleeting", "projects")
_HEALTH_WINDOW_HOURS = telemetry.HEALTH_WINDOW_HOURS
_DEEP_PROBE_TIMEOUT_SECONDS = 5
_DEEP_PROBE_QUERY = "memento-vault health probe"
_STALE_LOCK_SECONDS = 600
_INCEPTION_RECENT_RUNS_LIMIT = 5
_INCEPTION_ERROR_DETAIL_LIMIT = 500
_RECENT_FAILURE_ACTION_MARKERS = telemetry.RECENT_FAILURE_ACTION_MARKERS
_RETRIEVAL_SKIP_ACTIONS = telemetry.RETRIEVAL_SKIP_ACTIONS
_RETRIEVAL_NO_RESULT_REASONS = telemetry.RETRIEVAL_NO_RESULT_REASONS
_RETRIEVAL_BACKEND_UNAVAILABLE_REASONS = telemetry.RETRIEVAL_BACKEND_UNAVAILABLE_REASONS
_RETRIEVAL_BACKEND_EXCEPTION_ACTIONS = telemetry.RETRIEVAL_BACKEND_EXCEPTION_ACTIONS
_RETRIEVAL_ERROR_DETAIL_LIMIT = telemetry.RETRIEVAL_ERROR_DETAIL_LIMIT
_STALE_MCP_HINT = (
    "likely stale headless Claude MCP config; rerun ./install.sh --reinstall; "
    'copied hooks should use {"mcpServers": {}} for --mcp-config'
)
_REINSTALL_HINT = "rerun ./install.sh --reinstall"
_STALE_CERTAINTY_HINT = (
    f"likely stale installed memento package; {_REINSTALL_HINT}; current triage accepts certainty labels like confirmed"
)
_ACCEPTED_CERTAINTY_LABELS = {
    "speculation",
    "speculative",
    "uncertain",
    "low",
    "medium",
    "moderate",
    "likely",
    "confirmed",
    "certain",
    "high",
    "proven",
    "verified",
}
_DEFAULT_CONFIG = {
    "vault_path": str(Path.home() / "memento"),
    "auto_commit": True,
    "search_backend": "auto",
    "search_db_path": ".search/search.db",
    "inception_enabled": False,
    "queue_backlog_warn_threshold": 10,
    "queue_backlog_fail_threshold": 50,
    "queue_oldest_age_warn_hours": 24,
    "queue_oldest_age_fail_hours": 72,
}
RETRIEVAL_LOG_PATH = str(
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "memento-vault" / "retrieval.jsonl"
)
TRIAGE_HEALTH_LOG_PATH = str(
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "memento-vault" / "triage-health.jsonl"
)
AUTOMATION_MEMORY_HEALTH_LOG_PATH = str(
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "memento-vault"
    / "automation-memory-health.jsonl"
)
INCEPTION_STATE_PATH = str(
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "memento-vault" / "inception-state.json"
)
_DEFAULT_RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", Path.home() / ".cache")) / "memento-vault"
VAULT_WRITE_LOCK_PATH = str(_DEFAULT_RUNTIME_DIR / "vault-write.lock")
INCEPTION_LOCK_PATH = str(_DEFAULT_RUNTIME_DIR / "inception.lock")


@dataclass
class CheckResult:
    """One health check result."""

    name: str
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"unknown health status: {self.status}")
        self.message = _safe_text(self.message)
        self.details = _sanitize_obj(self.details)


@dataclass
class HealthReport:
    """Aggregated health report."""

    status: str
    summary: dict[str, int]
    checks: list[CheckResult]
    automation_memory: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, verbose: bool = True) -> dict[str, Any]:
        checks = []
        for check in self.checks:
            item = asdict(check)
            if not verbose:
                item.pop("details", None)
            checks.append(item)
        payload = {"status": self.status, "summary": dict(self.summary), "checks": checks}
        if self.automation_memory:
            payload["automation_memory"] = self.automation_memory
        return payload


def build_report(*, deep: bool = False, probe_timeout_seconds: int = _DEEP_PROBE_TIMEOUT_SECONDS) -> HealthReport:
    """Run cheap, read-only health checks."""
    probe_timeout_seconds = max(1, int(probe_timeout_seconds))
    checks: list[CheckResult] = []
    config_check, config = _check_config_parse()
    checks.append(config_check)

    vault = Path(config.get("vault_path") or _DEFAULT_CONFIG["vault_path"]).expanduser()
    checks.append(_check_vault_dirs(vault))
    checks.append(_check_git(vault, config))
    search_check = _check_search_backend(vault, config)
    checks.append(search_check)
    manifest_check, manifest = _check_install_manifest()
    checks.append(manifest_check)
    checks.append(_check_managed_files(manifest))
    checks.append(_check_claude_hooks(manifest))
    checks.extend(_check_mcp_config())
    checks.append(_check_mcp_registration())
    checks.append(_check_pi_bridge_config())
    checks.append(_check_pi_bridge_health())
    checks.append(_check_queue_health(config))
    checks.append(_check_triage_health())
    checks.append(_check_local_extraction_retries(vault))
    checks.append(_check_retrieval_health(config=config, search_check=search_check))
    checks.append(_check_locks())
    checks.append(_check_inception(config))
    if deep:
        checks.extend(_check_deep_diagnostics(config=config, vault=vault, probe_timeout_seconds=probe_timeout_seconds))
    automation_memory = build_automation_memory_readiness(config=config, vault=vault, checks=checks)
    checks.append(
        CheckResult(
            "automation memory",
            automation_memory["status"],
            automation_memory["message"],
            automation_memory["metadata"],
        )
    )

    summary = {status: sum(1 for check in checks if check.status == status) for status in _STATUSES}
    status = FAIL if summary[FAIL] else WARN if summary[WARN] else PASS
    return HealthReport(status=status, summary=summary, checks=checks, automation_memory=automation_memory)


def _check_deep_diagnostics(*, config: dict[str, Any], vault: Path, probe_timeout_seconds: int) -> list[CheckResult]:
    checks: list[CheckResult] = []
    checks.append(_check_deep_search_probe(config=config, probe_timeout_seconds=probe_timeout_seconds))
    checks.append(_check_deep_mcp_probe(vault=vault, probe_timeout_seconds=probe_timeout_seconds))
    checks.append(_check_deep_pi_bridge_probe(vault=vault, probe_timeout_seconds=probe_timeout_seconds))
    if remote_client.is_remote():
        checks.append(_check_deep_remote_probe(probe_timeout_seconds=probe_timeout_seconds))
    return checks


def _check_deep_search_probe(*, config: dict[str, Any], probe_timeout_seconds: int) -> CheckResult:
    reset_backend()
    backend = get_backend()
    collection = str(config.get("qmd_collection") or "memento")
    start = time.monotonic()
    try:
        results = backend.search(
            _DEEP_PROBE_QUERY,
            collection,
            limit=1,
            timeout=probe_timeout_seconds,
            min_score=0.0,
            concrete=True,
        )
    except Exception as exc:
        return CheckResult(
            "deep search probe",
            WARN,
            f"selected search backend probe failed: {exc}",
            {"timeout_seconds": probe_timeout_seconds, "error": str(exc)},
        )
    latency_ms = int((time.monotonic() - start) * 1000)
    return CheckResult(
        "deep search probe",
        PASS,
        f"selected search backend answered probe query ({len(results)} result(s))",
        {
            "query": _DEEP_PROBE_QUERY,
            "timeout_seconds": probe_timeout_seconds,
            "latency_ms": latency_ms,
            "backend": type(backend).__name__,
            "collection": collection,
            "result_count": len(results),
            "first_result": _sanitize_obj(results[0]) if results else None,
        },
    )


def _check_deep_mcp_probe(*, vault: Path, probe_timeout_seconds: int) -> CheckResult:
    start = time.monotonic()
    try:
        from memento import mcp_server

        status = mcp_server.memento_status()
        search = mcp_server.memento_search(_DEEP_PROBE_QUERY, limit=1, semantic=False, min_score=0.0, cwd=str(vault))
    except Exception as exc:
        return CheckResult(
            "deep mcp probe",
            WARN,
            f"local MCP tool probe failed: {exc}",
            {"timeout_seconds": probe_timeout_seconds, "error": str(exc)},
        )
    latency_ms = int((time.monotonic() - start) * 1000)
    result_count = (
        len(search) if isinstance(search, list) else len(search.get("results", [])) if isinstance(search, dict) else 0
    )
    return CheckResult(
        "deep mcp probe",
        PASS,
        "local MCP tools responded to probe calls",
        {
            "timeout_seconds": probe_timeout_seconds,
            "latency_ms": latency_ms,
            "status": _sanitize_obj(status),
            "search_result_count": result_count,
            "search": _sanitize_obj(search),
        },
    )


def _check_deep_pi_bridge_probe(*, vault: Path, probe_timeout_seconds: int) -> CheckResult:
    env = os.environ.copy()
    repo_root = str(_repo_root())
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else repo_root
    start = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "memento.pi_bridge", "status", "--cwd", str(vault)],
            text=True,
            capture_output=True,
            timeout=probe_timeout_seconds,
            cwd=repo_root,
            env=env,
            check=False,
        )
    except Exception as exc:
        return CheckResult(
            "deep pi bridge probe",
            WARN,
            f"Pi bridge status probe failed: {exc}",
            {"timeout_seconds": probe_timeout_seconds, "error": str(exc)},
        )
    latency_ms = int((time.monotonic() - start) * 1000)
    stdout = _safe_text(completed.stdout.strip())
    stderr = _safe_text(completed.stderr.strip())
    if completed.returncode != 0:
        return CheckResult(
            "deep pi bridge probe",
            WARN,
            f"Pi bridge status probe exited {completed.returncode}",
            {
                "timeout_seconds": probe_timeout_seconds,
                "latency_ms": latency_ms,
                "returncode": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
            },
        )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return CheckResult(
            "deep pi bridge probe",
            WARN,
            f"Pi bridge status probe returned invalid JSON: {exc}",
            {
                "timeout_seconds": probe_timeout_seconds,
                "latency_ms": latency_ms,
                "stdout": stdout,
                "stderr": stderr,
            },
        )
    return CheckResult(
        "deep pi bridge probe",
        PASS,
        "Pi bridge status probe responded",
        {
            "timeout_seconds": probe_timeout_seconds,
            "latency_ms": latency_ms,
            "status": _sanitize_obj(payload),
            "stdout": stdout,
            "stderr": stderr,
        },
    )


def _check_deep_remote_probe(*, probe_timeout_seconds: int) -> CheckResult:
    start = time.monotonic()
    try:
        status = remote_client.status(timeout=probe_timeout_seconds)
        search = remote_client.search_envelope(_DEEP_PROBE_QUERY, limit=1, timeout=probe_timeout_seconds)
    except Exception as exc:
        return CheckResult(
            "deep remote probe",
            WARN,
            f"remote vault probe failed: {exc}",
            {"timeout_seconds": probe_timeout_seconds, "error": str(exc)},
        )
    latency_ms = int((time.monotonic() - start) * 1000)
    if isinstance(status, dict) and status.get("error"):
        return CheckResult(
            "deep remote probe",
            WARN,
            f"remote vault status probe failed: {status['error']}",
            {
                "timeout_seconds": probe_timeout_seconds,
                "latency_ms": latency_ms,
                "status": _sanitize_obj(status),
                "search": _sanitize_obj(search),
            },
        )
    return CheckResult(
        "deep remote probe",
        PASS,
        "remote vault status and search probes responded",
        {
            "timeout_seconds": probe_timeout_seconds,
            "latency_ms": latency_ms,
            "status": _sanitize_obj(status),
            "search": _sanitize_obj(search),
        },
    )


def render_human(report: HealthReport, verbose: bool = False) -> str:
    """Render a concise human-readable report."""
    lines = [
        f"Memento Vault health: {report.status.upper()} "
        f"({report.summary[PASS]} pass, {report.summary[WARN]} warn, {report.summary[FAIL]} fail)"
    ]
    for check in report.checks:
        lines.append(f"[{check.status.upper()}] {check.name}: {check.message}")
        if verbose and check.details:
            for key, value in sorted(check.details.items()):
                rendered = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
                lines.append(f"  - {key}: {_safe_text(rendered)}")
    return "\n".join(lines)


def exit_code(report: HealthReport, strict: bool = False) -> int:
    """Return process exit code for report status."""
    if report.summary[FAIL] > 0:
        return 1
    if strict and report.summary[WARN] > 0:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check memento-vault operational health")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.add_argument("--verbose", action="store_true", help="Include sanitized details in human output")
    parser.add_argument("--strict", action="store_true", help="Exit nonzero when warnings are present")
    parser.add_argument("--deep", action="store_true", help="Run opt-in live integration probes")
    args = parser.parse_args(argv)

    report = build_report(deep=args.deep)
    if args.json:
        print(json.dumps(report.to_dict(verbose=True), indent=2, sort_keys=True))
    else:
        print(render_human(report, verbose=args.verbose))
    return exit_code(report, strict=args.strict)


def _check_config_parse() -> tuple[CheckResult, dict[str, Any]]:
    config_path = _first_config_path()
    if not config_path:
        config = _apply_env_overrides(dict(_DEFAULT_CONFIG))
        return CheckResult("config", PASS, "using default config", {"vault_path": config.get("vault_path")}), config

    config = _apply_env_overrides(dict(_DEFAULT_CONFIG))
    try:
        parsed = _read_config_file(config_path)
        config.update({k: v for k, v in parsed.items() if v is not None})
        config = _apply_env_overrides(config)
        config["vault_path"] = str(Path(str(config["vault_path"])).expanduser())
        return CheckResult("config", PASS, f"parsed {config_path}", {"path": str(config_path)}), config
    except Exception as exc:
        return (
            CheckResult(
                "config", FAIL, f"cannot parse {config_path}: {exc}", {"path": str(config_path), "error": str(exc)}
            ),
            config,
        )


def _apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("MEMENTO_VAULT_PATH"):
        config["vault_path"] = os.environ["MEMENTO_VAULT_PATH"]
    if os.environ.get("MEMENTO_SEARCH_BACKEND"):
        config["search_backend"] = os.environ["MEMENTO_SEARCH_BACKEND"]
    config["vault_path"] = str(Path(str(config["vault_path"])).expanduser())
    return config


def _first_config_path() -> Path | None:
    candidates = []
    vault_path = Path(os.environ.get("MEMENTO_VAULT_PATH", _DEFAULT_CONFIG["vault_path"])).expanduser()
    if vault_path.exists():
        candidates.append(vault_path / "memento.yml")
    candidates.extend(
        [
            Path.home() / ".config" / "memento-vault" / "memento.yml",
            Path.home() / ".memento-vault.yml",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _read_config_file(path: Path) -> dict[str, Any]:
    try:
        import yaml

        with path.open() as handle:
            data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            raise ValueError("config root must be a mapping")
        return data
    except ImportError:
        return _parse_simple_yaml(path)


def _parse_simple_yaml(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    with path.open() as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value.lower() in ("true", "yes"):
                parsed: Any = True
            elif value.lower() in ("false", "no"):
                parsed = False
            elif value.isdigit():
                parsed = int(value)
            elif value.startswith("["):
                if not value.endswith("]"):
                    raise ValueError(f"malformed list value in {path}: {value}")
                inner = value[1:-1].strip()
                parsed = [v.strip().strip('"').strip("'") for v in inner.split(",")] if inner else []
            elif (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                parsed = value[1:-1]
            else:
                parsed = value
            result[key] = parsed
    return result


def _check_vault_dirs(vault: Path) -> CheckResult:
    if not vault.exists():
        return CheckResult("vault", FAIL, f"vault path does not exist: {vault}", {"vault_path": str(vault)})
    if not vault.is_dir():
        return CheckResult("vault", FAIL, f"vault path is not a directory: {vault}", {"vault_path": str(vault)})

    present = [name for name in _EXPECTED_DIRS if (vault / name).is_dir()]
    missing = [name for name in _EXPECTED_DIRS if name not in present]
    if not any((vault / name).is_dir() for name in _CORE_DIRS):
        return CheckResult(
            "vault", FAIL, "vault has no usable notes/fleeting/projects directories", {"missing": missing}
        )
    if missing:
        return CheckResult(
            "vault", WARN, f"vault exists but missing expected dirs: {', '.join(missing)}", {"missing": missing}
        )
    return CheckResult("vault", PASS, f"vault directory structure looks usable at {vault}", {"vault_path": str(vault)})


def _check_git(vault: Path, config: dict[str, Any]) -> CheckResult:
    if not config.get("auto_commit", True):
        return CheckResult("git", PASS, "auto-commit disabled")
    if shutil.which("git") is None:
        return CheckResult("git", WARN, "auto-commit enabled but git is not on PATH")
    if not (vault / ".git").exists():
        return CheckResult(
            "git", WARN, "auto-commit enabled but vault is not a git repository", {"vault_path": str(vault)}
        )
    return CheckResult("git", PASS, "git repository detected for auto-commit")


def _check_search_backend(vault: Path, config: dict[str, Any]) -> CheckResult:
    choice = str(config.get("search_backend", "auto"))
    qmd_present = shutil.which("qmd") is not None
    embedded_db = vault / str(config.get("search_db_path", ".search/search.db"))
    grep_ready = any((vault / dirname).is_dir() for dirname in _CORE_DIRS)

    if choice == "qmd":
        if not qmd_present:
            return CheckResult("search", FAIL, "search_backend is qmd but qmd is not on PATH")
        return CheckResult("search", PASS, "qmd backend selected and qmd is on PATH")
    if choice == "embedded":
        if not vault.exists():
            return CheckResult("search", FAIL, "search_backend is embedded but vault path is missing")
        if not embedded_db.exists():
            return CheckResult(
                "search",
                WARN,
                "embedded search selected but index database is not present",
                {"db_path": str(embedded_db)},
            )
        return CheckResult("search", PASS, "embedded search index detected", {"db_path": str(embedded_db)})
    if choice == "grep":
        if not grep_ready:
            return CheckResult("search", FAIL, "search_backend is grep but vault content directories are missing")
        return CheckResult("search", PASS, "grep backend can read vault content directories")
    if choice != "auto":
        return CheckResult("search", WARN, f"unknown search_backend '{choice}'; runtime will fall back if possible")

    if qmd_present:
        return CheckResult("search", PASS, "search_backend auto can use qmd")
    if embedded_db.exists():
        return CheckResult("search", PASS, "search_backend auto can use embedded index", {"db_path": str(embedded_db)})
    if grep_ready:
        return CheckResult(
            "search", WARN, "search_backend auto will fall back to grep; semantic search may be unavailable"
        )
    return CheckResult("search", FAIL, "search_backend auto found no usable local backend")


def build_automation_memory_readiness(
    *,
    config: dict[str, Any] | None = None,
    vault: Path | None = None,
    checks: list[CheckResult] | None = None,
    qmd_available: bool | None = None,
) -> dict[str, Any]:
    """Build cheap, read-only automation-memory readiness metadata.

    The payload is designed for orchestration probes (for example Rondo): it is
    secret-sanitized, never performs network I/O, and reports degraded memory as
    explicit metadata rather than making optional memory fail closed by default.
    """
    if config is None:
        _config_check, config = _check_config_parse()
    vault = vault or Path(config.get("vault_path") or _DEFAULT_CONFIG["vault_path"]).expanduser()
    checks = checks or []
    search_check = next((check for check in checks if check.name == "search"), None) or _check_search_backend(
        vault, config
    )
    search = _automation_search_metadata(vault, config, search_check, qmd_available=qmd_available)
    recall = _automation_recall_metadata(config=config, search_check=search_check)
    remote_sync = _automation_remote_sync_metadata(vault)
    local_extraction_retries = _local_extraction_retry_metadata(vault)
    last_packet = _last_successful_automation_packet()
    common_failure_reasons = _common_automation_failure_reasons(vault)

    readiness = "ready"
    status = PASS
    blockers: list[str] = []
    degradations: list[str] = []
    if not vault.exists():
        readiness = "unavailable"
        status = FAIL
        blockers.append("vault_missing")
    if not search["available"]:
        readiness = "unavailable"
        status = FAIL
        blockers.append("search_backend_unavailable")
    elif search["status"] == WARN:
        readiness = "degraded"
        status = WARN
        degradations.append("search_backend_degraded")
    if search.get("stale_index", {}).get("stale"):
        if status != FAIL:
            status = WARN
            readiness = "degraded"
        degradations.append("stale_index")
    if telemetry.failure_rate_warns(recall["failures"], recall["events"]):
        if status != FAIL:
            status = WARN
            readiness = "degraded"
        degradations.append("recall_failure_rate")
    if remote_sync.get("pending_retry_count", 0):
        if status != FAIL:
            status = WARN
            readiness = "degraded"
        degradations.append("remote_sync_pending_retries")
    if local_extraction_retries.get("pending_retry_count", 0):
        if status != FAIL:
            status = WARN
            readiness = "degraded"
        degradations.append("local_extraction_pending_retries")
    if local_extraction_retries.get("dead_letter_count", 0):
        if status != FAIL:
            status = WARN
            readiness = "degraded"
        degradations.append("local_extraction_dead_letters")

    message = f"automation memory {readiness}"
    if degradations:
        message += f" ({', '.join(degradations)})"
    if blockers:
        message += f" ({', '.join(blockers)})"

    metadata: dict[str, Any] = {
        "schema_version": 1,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "window_hours": _HEALTH_WINDOW_HOURS,
        "cheap_read_only": True,
        "network_checked": False,
        "readiness": readiness,
        "fail_open_default": True,
        "search": search,
        "recall": recall,
        "remote_sync": remote_sync,
        "local_extraction_retries": local_extraction_retries,
        "last_successful_packet": last_packet,
        "common_failure_reasons": common_failure_reasons,
        "probe": {
            "name": "automation_memory",
            "version": 1,
            "readiness": readiness,
            "required_by_default": False,
        },
    }
    return {"ready": status != FAIL, "status": status, "message": message, "metadata": _sanitize_obj(metadata)}


def _automation_search_metadata(
    vault: Path, config: dict[str, Any], search_check: CheckResult, *, qmd_available: bool | None = None
) -> dict[str, Any]:
    choice = str(config.get("search_backend", "auto"))
    available = search_check.status != FAIL
    if qmd_available is not None:
        if choice == "qmd":
            available = bool(qmd_available)
        elif choice == "auto" and qmd_available:
            available = True
    return {
        "backend": choice,
        "available": available,
        "status": search_check.status,
        "message": search_check.message,
        "qmd_available": shutil.which("qmd") is not None if qmd_available is None else bool(qmd_available),
        "stale_index": _embedded_index_staleness(vault, config),
    }


def _embedded_index_staleness(vault: Path, config: dict[str, Any]) -> dict[str, Any]:
    db_path = vault / str(config.get("search_db_path", ".search/search.db"))
    metadata: dict[str, Any] = {
        "checked": True,
        "backend": "embedded",
        "db_path": str(db_path),
        "stale": False,
    }
    if not vault.exists():
        metadata.update({"checked": False, "reason": "vault_missing"})
        return metadata
    if not db_path.exists():
        metadata.update({"checked": False, "reason": "embedded_index_missing"})
        return metadata
    newest_note_mtime = None
    try:
        for dirname in _CORE_DIRS:
            root = vault / dirname
            if not root.exists():
                continue
            for path in root.rglob("*.md"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                newest_note_mtime = mtime if newest_note_mtime is None else max(newest_note_mtime, mtime)
        db_mtime = db_path.stat().st_mtime
    except OSError as exc:
        metadata.update({"checked": False, "reason": type(exc).__name__})
        return metadata
    if newest_note_mtime is None:
        metadata.update({"reason": "no_notes"})
        return metadata
    lag_seconds = int(max(0, newest_note_mtime - db_mtime))
    metadata.update(
        {
            "db_mtime": datetime.fromtimestamp(db_mtime).isoformat(timespec="seconds"),
            "newest_note_mtime": datetime.fromtimestamp(newest_note_mtime).isoformat(timespec="seconds"),
            "lag_seconds": lag_seconds,
            "stale": lag_seconds > 60,
        }
    )
    return metadata


def _automation_recall_metadata(
    *, config: dict[str, Any] | None = None, search_check: CheckResult | None = None
) -> dict[str, Any]:
    path = Path(RETRIEVAL_LOG_PATH)
    cutoff = datetime.now() - timedelta(hours=_HEALTH_WINDOW_HOURS)
    diagnostics = _scan_retrieval_logs(path, cutoff)
    status = WARN if telemetry.failure_rate_warns(diagnostics["failures"], diagnostics["events"]) else PASS
    metadata = {
        "log_path": str(path),
        "events": diagnostics["events"],
        "failures": diagnostics["failures"],
        "failure_rate": diagnostics["failure_rate"],
        "status": status,
        "last_error": diagnostics["last_error"],
        "last_error_truncated": diagnostics["last_error_truncated"],
        "no_results": diagnostics["no_results"],
        "backend_unavailable": diagnostics["backend_unavailable"],
        "backend_exceptions": diagnostics["backend_exceptions"],
        "low_signal_skips": diagnostics["low_signal_skips"],
        "other_failures": diagnostics["other_failures"],
    }
    remediation = _retrieval_remediation(diagnostics, config or {}, search_check)
    if remediation:
        metadata["remediation"] = remediation
    return metadata


def _local_extraction_retry_metadata(vault: Path) -> dict[str, Any]:
    ledger = vault / ".sync" / "ledger.jsonl"
    metadata: dict[str, Any] = {
        "checked": True,
        "check": "local_sync_ledger",
        "ledger_path": str(ledger),
        "pending_retry_count": 0,
        "dead_letter_count": 0,
    }
    if not ledger.exists():
        metadata["reason"] = "sync_ledger_missing"
        return metadata

    current: dict[tuple[str, str], dict[str, Any]] = {}
    error_count = dead_letter_count = ok_count = 0
    last_error = last_dead_letter = None
    for rec in _iter_jsonl(ledger):
        if rec.get("kind") != "local-extraction":
            continue
        source = str(rec.get("source") or "")
        if not source:
            continue
        status = rec.get("status")
        if status == "ok":
            ok_count += 1
        elif status == "error":
            error_count += 1
            last_error = rec.get("ts") or last_error
        elif status == "dead-letter":
            dead_letter_count += 1
            last_dead_letter = rec.get("ts") or last_dead_letter
        current[("local-extraction", source)] = rec

    pending = [rec for rec in current.values() if rec.get("status") == "error"]
    dead_letters = [rec for rec in current.values() if rec.get("status") == "dead-letter"]
    metadata.update(
        {
            "ok_count": ok_count,
            "error_count": error_count,
            "dead_letter_event_count": dead_letter_count,
            "pending_retry_count": len(pending),
            "dead_letter_count": len(dead_letters),
            "last_error_at": last_error,
            "last_dead_letter_at": last_dead_letter,
            "pending_sources": sorted(str(rec.get("source")) for rec in pending)[:20],
            "dead_letter_sources": sorted(str(rec.get("source")) for rec in dead_letters)[:20],
        }
    )
    return metadata


def _check_local_extraction_retries(vault: Path) -> CheckResult:
    metadata = _local_extraction_retry_metadata(vault)
    pending = int(metadata.get("pending_retry_count") or 0)
    dead_letters = int(metadata.get("dead_letter_count") or 0)
    if dead_letters:
        status = WARN
        message = f"local extraction retry backlog has {pending} pending and {dead_letters} dead-lettered item(s)"
    elif pending:
        status = WARN
        message = f"local extraction retry backlog has {pending} pending item(s)"
    else:
        status = PASS
        message = "local extraction retry backlog is empty"
    return CheckResult("local extraction retries", status, message, metadata)


def _automation_remote_sync_metadata(vault: Path) -> dict[str, Any]:
    remote_configured = bool(os.environ.get("MEMENTO_VAULT_URL"))
    metadata: dict[str, Any] = {
        "remote_configured": remote_configured,
        "checked": remote_configured,
        "check": "local_sync_ledger",
        "network_checked": False,
        "pending_retry_count": 0,
    }
    if not remote_configured:
        metadata["reason"] = "remote_not_configured"
        return metadata
    ledger = vault / ".sync" / "ledger.jsonl"
    metadata["ledger_path"] = str(ledger)
    if not ledger.exists():
        metadata["reason"] = "sync_ledger_missing"
        return metadata
    current: dict[tuple[str, str], dict[str, Any]] = {}
    ok_count = error_count = 0
    last_ok = last_error = None
    for rec in _iter_jsonl(ledger):
        kind = str(rec.get("kind") or "")
        source = str(rec.get("source") or "")
        if not kind or not source:
            continue
        status = rec.get("status")
        if status == "ok":
            ok_count += 1
            last_ok = rec.get("ts") or last_ok
        elif status == "error":
            error_count += 1
            last_error = rec.get("ts") or last_error
        current[(kind, source)] = rec
    pending = [rec for rec in current.values() if rec.get("status") == "error"]
    metadata.update(
        {
            "ok_count": ok_count,
            "error_count": error_count,
            "pending_retry_count": len(pending),
            "last_success_at": last_ok,
            "last_error_at": last_error,
            "pending_kinds": sorted({str(rec.get("kind")) for rec in pending}),
        }
    )
    return metadata


def _last_successful_automation_packet() -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    latest_ts: datetime | None = None
    for rec in _iter_jsonl(Path(AUTOMATION_MEMORY_HEALTH_LOG_PATH)):
        if rec.get("hook") != "automation-memory" or rec.get("action") != "packet_success":
            continue
        ts = _parse_ts(rec.get("ts"))
        if ts is None:
            continue
        if latest_ts is None or ts >= latest_ts:
            latest_ts = ts
            latest = {
                "ts": rec.get("ts"),
                "source": rec.get("source"),
                "should_inject": bool(rec.get("should_inject")),
                "result_count": int(rec.get("result_count") or 0),
                "warning_count": int(rec.get("warning_count") or 0),
                "truncated": bool(rec.get("truncated")),
            }
    return latest


def _common_automation_failure_reasons(vault: Path, limit: int = 5) -> list[dict[str, Any]]:
    cutoff = datetime.now() - timedelta(hours=_HEALTH_WINDOW_HOURS)
    counts: Counter[str] = Counter()
    for path in (Path(RETRIEVAL_LOG_PATH), Path(TRIAGE_HEALTH_LOG_PATH), vault / ".sync" / "ledger.jsonl"):
        for rec in _iter_recent_jsonl(path, cutoff):
            reason = None
            if rec.get("status") == "error":
                reason = rec.get("error") or "sync_error"
            else:
                action = str(rec.get("action") or "")
                if any(marker in action for marker in _RECENT_FAILURE_ACTION_MARKERS):
                    reason = rec.get("error") or rec.get("reason") or action
            if reason:
                counts[_safe_text(str(reason))] += 1
    return [{"reason": reason, "count": count} for reason, count in counts.most_common(limit)]


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "memento-vault"


def _home_config_dir() -> Path:
    return Path.home() / ".config" / "memento-vault"


def _config_file_path(filename: str) -> Path:
    xdg_path = _config_dir() / filename
    home_path = _home_config_dir() / filename
    if xdg_path.exists() or xdg_path == home_path:
        return xdg_path
    return home_path


def _install_manifest_path() -> Path:
    return _config_file_path("manifest.json")


def _check_install_manifest() -> tuple[CheckResult, dict[str, Any] | None]:
    path = _install_manifest_path()
    if not path.exists():
        return CheckResult(
            "install manifest", WARN, f"install manifest not found; {_REINSTALL_HINT}", {"path": str(path)}
        ), None
    try:
        manifest = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return (
            CheckResult(
                "install manifest",
                WARN,
                f"cannot read install manifest at {path}: {exc}; {_REINSTALL_HINT}",
                {"path": str(path), "error": str(exc)},
            ),
            None,
        )
    if not isinstance(manifest, dict):
        return CheckResult(
            "install manifest", WARN, f"install manifest is not an object; {_REINSTALL_HINT}", {"path": str(path)}
        ), None

    options = manifest.get("options") if isinstance(manifest.get("options"), dict) else {}
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    version = str(manifest.get("version") or "unknown")
    return (
        CheckResult(
            "install manifest",
            PASS,
            f"install manifest v{version} found with {len(files)} managed files",
            {"path": str(path), "version": version, "options": sorted(options), "file_count": len(files)},
        ),
        manifest,
    )


_CRITICAL_MANAGED_KEYS = {
    "memento/__init__.py",
    "memento/config.py",
    "memento/utils.py",
    "memento/store.py",
    "memento/search.py",
    "memento/lifecycle.py",
    "memento/pi_bridge.py",
    "memento/adapters/__init__.py",
    "memento/adapters/claude.py",
    "memento/adapters/opencode.py",
    "memento/adapters/pi.py",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _file_sha256(path: Path) -> str | None:
    try:
        import hashlib

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _managed_source_path(key: str) -> Path | None:
    root = _repo_root()
    if key.startswith("hooks/") or key.startswith("memento/"):
        return root / key
    if key.startswith("skills/"):
        parts = key.split("/")
        if len(parts) == 2:
            return root / "skills" / parts[1] / "SKILL.md"
        return root / key
    if key.startswith("agents/"):
        return root / "agents" / f"{Path(key).name}.md"
    if key.startswith("codex-skills/"):
        skill = key.split("/", 1)[1]
        generic = root / "skills" / "generic" / skill / "SKILL.md"
        if generic.exists():
            return generic
        return root / "skills" / skill / "SKILL.md"
    return None


def _managed_dest_path(key: str) -> Path | None:
    if key.startswith("hooks/"):
        return Path.home() / ".claude" / key
    if key.startswith("memento/"):
        rel = key.split("/", 1)[1]
        return Path.home() / ".claude" / "hooks" / "memento" / rel
    if key.startswith("skills/"):
        parts = key.split("/")
        if len(parts) == 2:
            return Path.home() / ".claude" / "skills" / parts[1] / "SKILL.md"
        return Path.home() / ".claude" / key
    if key.startswith("agents/"):
        return Path.home() / ".claude" / "agents" / f"{Path(key).name}.md"
    if key.startswith("codex-skills/"):
        skill = key.split("/", 1)[1]
        return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills" / skill / "SKILL.md"
    return None


def _check_managed_files(manifest: dict[str, Any] | None) -> CheckResult:
    if not manifest:
        return CheckResult(
            "managed files", WARN, f"managed file drift unavailable without install manifest; {_REINSTALL_HINT}"
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        return CheckResult("managed files", WARN, f"install manifest has no managed file hashes; {_REINSTALL_HINT}")

    current: list[str] = []
    stale_managed: list[str] = []
    locally_modified: list[str] = []
    missing: list[str] = []
    missing_critical: list[str] = []
    source_missing: list[str] = []
    not_executable: list[str] = []

    for key, manifest_hash in sorted(files.items()):
        key = str(key)
        dest = _managed_dest_path(key)
        source = _managed_source_path(key)
        if dest is None:
            continue
        if not dest.exists():
            if key in _CRITICAL_MANAGED_KEYS:
                missing_critical.append(key)
            else:
                missing.append(key)
            continue
        if key == "hooks/vault-commit.sh" and not os.access(dest, os.X_OK):
            not_executable.append(key)
        installed_hash = _file_sha256(dest)
        source_hash = _file_sha256(source) if source is not None else None
        if source_hash is None:
            source_missing.append(key)
            continue
        if installed_hash == source_hash:
            current.append(key)
        elif manifest_hash and installed_hash == str(manifest_hash):
            stale_managed.append(key)
        else:
            locally_modified.append(key)

    worst = (
        FAIL
        if missing_critical
        else WARN
        if (stale_managed or locally_modified or missing or source_missing or not_executable)
        else PASS
    )
    if worst == PASS:
        message = f"managed installed files match this checkout ({len(current)} checked)"
    else:
        parts = []
        if missing_critical:
            parts.append(f"{len(missing_critical)} critical missing")
        if missing:
            parts.append(f"{len(missing)} missing")
        if stale_managed:
            parts.append(f"{len(stale_managed)} stale managed")
        if locally_modified:
            parts.append(f"{len(locally_modified)} locally modified")
        if not_executable:
            parts.append(f"{len(not_executable)} not executable")
        if source_missing:
            parts.append(f"{len(source_missing)} source missing")
        message = f"managed file drift detected: {', '.join(parts)}; {_REINSTALL_HINT}"

    return CheckResult(
        "managed files",
        worst,
        message,
        {
            "checked": len(files),
            "current_count": len(current),
            "missing": missing,
            "missing_critical": missing_critical,
            "stale_managed": stale_managed,
            "locally_modified": locally_modified,
            "not_executable": not_executable,
            "source_missing": source_missing,
        },
    )


def _expected_claude_hooks(manifest: dict[str, Any] | None) -> list[tuple[str, str]]:
    # Retrieval hooks are part of the default posture now; experimental install
    # options only gate the extra add-ons elsewhere in the installer.
    return [
        ("SessionEnd", "memento-triage.py"),
        ("SessionStart", "vault-briefing.py"),
        ("UserPromptSubmit", "vault-recall.py"),
        ("PreToolUse", "vault-tool-context.py"),
    ]


def _check_claude_hooks(manifest: dict[str, Any] | None) -> CheckResult:
    settings_path = Path.home() / ".claude" / "settings.json"
    expected = _expected_claude_hooks(manifest)
    if not settings_path.exists():
        return CheckResult(
            "claude hooks",
            WARN,
            f"Claude settings.json not found; {_REINSTALL_HINT}",
            {"path": str(settings_path), "expected": [f"{event}/{script}" for event, script in expected]},
        )
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return CheckResult(
            "claude hooks",
            WARN,
            f"cannot read Claude settings at {settings_path}: {exc}; {_REINSTALL_HINT}",
            {"path": str(settings_path), "error": str(exc)},
        )

    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    missing = []
    registered = []
    for event, script in expected:
        found = False
        entries = hooks.get(event, []) if isinstance(hooks, dict) else []
        if isinstance(entries, list):
            for entry in entries:
                hook_list = entry.get("hooks", [entry]) if isinstance(entry, dict) else []
                for hook in hook_list:
                    command = hook.get("command", "") if isinstance(hook, dict) else ""
                    if script in command:
                        found = True
        label = f"{event}/{script}"
        if found:
            registered.append(label)
        else:
            missing.append(label)
    if missing:
        return CheckResult(
            "claude hooks",
            WARN,
            f"missing Claude hook registrations: {', '.join(missing)}; {_REINSTALL_HINT}",
            {"path": str(settings_path), "registered": registered, "missing": missing},
        )
    return CheckResult(
        "claude hooks",
        PASS,
        "expected Claude hook registrations found",
        {"path": str(settings_path), "registered": registered},
    )


def _mcp_entry_shape(data: Any) -> tuple[str, str | None]:
    if not isinstance(data, dict):
        return "invalid", "MCP config root must be an object"
    entry = data.get("memento-vault")
    if entry is None:
        return "missing", None
    if not isinstance(entry, dict):
        return "invalid", "memento-vault entry must be an object"
    url = entry.get("url")
    if entry.get("type") == "http" or url is not None:
        if (
            not isinstance(url, str)
            or not url.startswith(("http://", "https://"))
            or not url.rstrip("/").endswith("/mcp")
        ):
            return "invalid", "remote MCP entry must have an http(s) url ending in /mcp"
        return "remote http", None
    args = entry.get("args")
    env = entry.get("env")
    command = entry.get("command")
    command_name = Path(str(command)).name if command else ""
    python_command = command_name.startswith("python")
    has_module_args = isinstance(args, list) and any(
        args[index] == "-m" and index + 1 < len(args) and args[index + 1] == "memento" for index in range(len(args))
    )
    if python_command and has_module_args and isinstance(env, dict) and env.get("PYTHONPATH"):
        return "local stdio", None
    return "invalid", "local MCP entry must run python3 -m memento with PYTHONPATH"


def _mcp_registration_shape(output: str) -> str:
    import re

    text = output.lower()
    if "memento-vault" not in text:
        return "invalid"
    urls = re.findall(r"https?://[^\s]+", text)
    if urls:
        return "remote http" if any(url.rstrip("/").endswith("/mcp") for url in urls) else "invalid"
    if re.search(r"\bpython(?:3(?:\.\d+)?)?\b", text) and re.search(r"(?:^|\s)-m\s+memento(?:\s|$)", text):
        return "local stdio"
    return "invalid"


def _check_mcp_registration() -> CheckResult:
    results = []
    worst = PASS
    for client in ("claude", "codex"):
        if shutil.which(client) is None:
            results.append({"client": client, "status": "skipped", "reason": "cli not found"})
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="memento-health-mcp-check-") as scratch_dir:
                # `claude`/`codex mcp get` reports live connectivity, which means
                # it actually spawns the registered MCP server process (memento
                # itself) to probe it. That spawned process is free to write its
                # own runtime/cache state; a read-only health check must not let
                # it land in this machine's real XDG runtime/cache dirs, so give
                # it an ephemeral scratch directory instead and discard it
                # afterward. This keeps the check itself side-effect free
                # regardless of how well-behaved the probed server is.
                probe_env = os.environ.copy()
                probe_env["XDG_RUNTIME_DIR"] = scratch_dir
                probe_env["XDG_CACHE_HOME"] = scratch_dir
                completed = subprocess.run(
                    [client, "mcp", "get", "memento-vault"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=3,
                    check=False,
                    env=probe_env,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            results.append({"client": client, "status": WARN, "reason": type(exc).__name__})
            worst = WARN
            continue
        if completed.returncode != 0:
            reason = _safe_text((completed.stderr or completed.stdout).strip()) or "registration lookup failed"
            results.append({"client": client, "status": WARN, "reason": reason})
            worst = WARN
            continue
        shape = _mcp_registration_shape(completed.stdout)
        if shape == "invalid":
            results.append({"client": client, "status": WARN, "reason": "unexpected registration shape"})
            worst = WARN
        else:
            results.append({"client": client, "status": PASS, "shape": shape})

    checked = [result for result in results if result["status"] != "skipped"]
    if worst == WARN:
        return CheckResult(
            "mcp registration",
            WARN,
            "MCP CLI registration missing or stale; run ./install.sh --mcp",
            {"registrations": results},
        )
    if checked:
        return CheckResult("mcp registration", PASS, "MCP CLI registration detected", {"registrations": results})
    return CheckResult(
        "mcp registration",
        PASS,
        "MCP CLI registration not checked (Claude/Codex CLI not found)",
        {"registrations": results},
    )


def _check_mcp_config() -> list[CheckResult]:
    checks = []
    config_path = Path.home() / ".claude" / "mcp-servers.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
            shape, error = _mcp_entry_shape(data)
            if shape == "invalid":
                checks.append(
                    CheckResult(
                        "mcp config",
                        FAIL,
                        f"invalid memento-vault MCP config at {config_path}: {error}; run ./install.sh --mcp",
                        {"path": str(config_path), "error": error},
                    )
                )
            elif shape == "missing":
                checks.append(
                    CheckResult(
                        "mcp config",
                        WARN,
                        f"memento-vault MCP server is not configured in {config_path}; run ./install.sh --mcp",
                        {"path": str(config_path)},
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        "mcp config",
                        PASS,
                        f"valid memento-vault MCP config at {config_path} ({shape})",
                        {"path": str(config_path), "memento_vault": shape},
                    )
                )
        except json.JSONDecodeError as exc:
            checks.append(
                CheckResult("mcp config", FAIL, f"invalid JSON at {config_path}: {exc}", {"path": str(config_path)})
            )
        except (OSError, UnicodeError) as exc:
            checks.append(
                CheckResult(
                    "mcp config",
                    FAIL,
                    f"cannot read MCP config at {config_path}: {exc}",
                    {"path": str(config_path), "error": str(exc)},
                )
            )
    else:
        checks.append(
            CheckResult(
                "mcp config", WARN, "Claude MCP config not found; run ./install.sh --mcp", {"path": str(config_path)}
            )
        )

    stale_paths = []
    for path in (Path.home() / ".claude" / "hooks" / "memento" / "llm.py", Path(__file__).with_name("llm.py")):
        if _has_stale_empty_mcp_config(path):
            stale_paths.append(str(path))
    if stale_paths:
        checks.append(CheckResult("headless claude mcp", WARN, _STALE_MCP_HINT, {"paths": stale_paths}))
    else:
        checks.append(CheckResult("headless claude mcp", PASS, "headless Claude empty MCP config shape looks current"))
    return checks


_PI_BOOL_KEYS = {
    "enabled",
    "briefing",
    "promptRecall",
    "toolContext",
    "autoCapture",
    "captureQueue",
    "processQueue",
    "processQueueOnSessionClose",
}
_PI_INT_KEYS = {"maxInjectedChars", "maxToolContextPerSession", "processQueueMaxCaptures"}
_PI_STRING_OR_NULL_KEYS = {"processQueueModel"}


def _check_pi_bridge_config() -> CheckResult:
    path = _config_file_path("pi-bridge.json")
    if not path.exists():
        return CheckResult("pi bridge", PASS, "Pi bridge config not found (optional)", {"path": str(path)})
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return CheckResult(
            "pi bridge", WARN, f"cannot read Pi bridge config at {path}: {exc}", {"path": str(path), "error": str(exc)}
        )
    if not isinstance(raw, dict):
        return CheckResult("pi bridge", WARN, "Pi bridge config root must be an object", {"path": str(path)})
    memento = raw.get("memento") if isinstance(raw.get("memento"), dict) else None
    candidate = (memento or {}).get("piBridge") if memento else raw.get("piBridge", raw)
    if not isinstance(candidate, dict):
        return CheckResult("pi bridge", WARN, "Pi bridge config must be an object", {"path": str(path)})
    invalid = []
    for key in sorted(_PI_BOOL_KEYS):
        if key in candidate and not isinstance(candidate[key], bool):
            invalid.append(key)
    for key in sorted(_PI_INT_KEYS):
        value = candidate.get(key)
        if key in candidate and (type(value) is not int or value < 0):
            invalid.append(key)
    for key in sorted(_PI_STRING_OR_NULL_KEYS):
        value = candidate.get(key)
        if key in candidate and not (isinstance(value, str) or value is None):
            invalid.append(key)
    if invalid:
        return CheckResult(
            "pi bridge",
            WARN,
            f"Pi bridge config has invalid key types: {', '.join(invalid)}",
            {"path": str(path), "invalid_keys": invalid},
        )
    configured = sorted(
        key for key in candidate if key in _PI_BOOL_KEYS or key in _PI_INT_KEYS or key in _PI_STRING_OR_NULL_KEYS
    )
    return CheckResult(
        "pi bridge", PASS, "Pi bridge config shape looks valid", {"path": str(path), "configured_keys": configured}
    )


_PI_BRIDGE_FAILURE_ACTIONS = telemetry.PI_BRIDGE_FAILURE_ACTIONS


def _is_pi_bridge_failure_record(rec: dict[str, Any]) -> bool:
    return telemetry.is_pi_bridge_failure_record(rec)


def _check_pi_bridge_health() -> CheckResult:
    path = Path(TRIAGE_HEALTH_LOG_PATH)
    cutoff = datetime.now() - timedelta(hours=_HEALTH_WINDOW_HOURS)
    if not path.exists():
        return CheckResult(
            "pi bridge health",
            PASS,
            "no recent Pi bridge failures recorded",
            {"log_path": str(path), "window_hours": _HEALTH_WINDOW_HOURS},
        )

    recent_failures: list[dict[str, Any]] = []
    latest_failure: dict[str, Any] | None = None
    for rec in _iter_recent_jsonl(path, cutoff):
        if rec.get("hook") != "pi-bridge" or not _is_pi_bridge_failure_record(rec):
            continue
        failure = {
            "ts": rec.get("ts"),
            "action": rec.get("action"),
            "operation": rec.get("operation") or rec.get("action"),
            "backend": rec.get("backend"),
            "config": rec.get("config"),
            "cwd": rec.get("cwd"),
            "project": rec.get("project"),
            "session_id": rec.get("session_id"),
            "error": rec.get("error"),
            "error_type": rec.get("error_type"),
            "reason": rec.get("reason"),
        }
        recent_failures.append(_sanitize_obj(failure))
        latest_failure = failure

    if not recent_failures:
        return CheckResult(
            "pi bridge health",
            PASS,
            "no recent Pi bridge failures recorded",
            {"log_path": str(path), "window_hours": _HEALTH_WINDOW_HOURS},
        )

    last_error = _safe_text(str((latest_failure or {}).get("error") or ""))
    message = f"Pi bridge failures {len(recent_failures)} in last {_HEALTH_WINDOW_HOURS}h"
    if last_error:
        message += f' — last error: "{last_error}"'
    details: dict[str, Any] = {
        "log_path": str(path),
        "window_hours": _HEALTH_WINDOW_HOURS,
        "events": len(recent_failures),
        "failures": len(recent_failures),
        "last_failure": _sanitize_obj(latest_failure or {}),
        "recent_failures": recent_failures[:5],
    }
    return CheckResult("pi bridge health", WARN, message, details)


def _check_queue_health(config: dict[str, Any]) -> CheckResult:
    queue_path = _pi_queue_file()
    thresholds = _queue_health_thresholds(config)
    details: dict[str, Any] = {
        "queue_path": str(queue_path),
        "queued_capture_count": 0,
        "parsed_capture_count": 0,
        "unparsed_capture_count": 0,
        "oldest_capture_at": None,
        "oldest_capture_age_hours": None,
        "thresholds": thresholds,
    }
    if not queue_path.exists():
        return CheckResult("queue health", PASS, "capture queue is empty", details)

    now = datetime.now(timezone.utc)
    count = parsed_count = unparsed_count = 0
    oldest_at: datetime | None = None
    oldest_capture: dict[str, Any] | None = None
    for rec in _iter_jsonl(queue_path):
        count += 1
        created_at = str(rec.get("created_at") or rec.get("date") or "")
        ts = telemetry.parse_timestamp_utc(created_at)
        if ts is None:
            unparsed_count += 1
            continue
        parsed_count += 1
        if oldest_at is None or ts < oldest_at:
            oldest_at = ts
            oldest_capture = rec

    details["queued_capture_count"] = count
    details["parsed_capture_count"] = parsed_count
    details["unparsed_capture_count"] = unparsed_count
    if oldest_at is not None:
        details["oldest_capture_at"] = telemetry.format_timestamp_utc(oldest_at)
        details["oldest_capture_age_hours"] = round((now - oldest_at).total_seconds() / 3600, 2)
        if isinstance(oldest_capture, dict) and oldest_capture.get("id"):
            details["oldest_capture_id"] = str(oldest_capture["id"])

    backlog_warn = thresholds["queue_backlog_warn_threshold"]
    backlog_fail = thresholds["queue_backlog_fail_threshold"]
    age_warn = thresholds["queue_oldest_age_warn_hours"]
    age_fail = thresholds["queue_oldest_age_fail_hours"]
    status = PASS
    reasons: list[str] = []
    if count >= backlog_fail:
        status = FAIL
        reasons.append(f"backlog {count} >= fail threshold {backlog_fail}")
    elif count >= backlog_warn:
        status = WARN
        reasons.append(f"backlog {count} >= warn threshold {backlog_warn}")

    oldest_age_hours = details["oldest_capture_age_hours"]
    if isinstance(oldest_age_hours, (int, float)):
        if oldest_age_hours >= age_fail:
            status = FAIL
            reasons.append(f"oldest entry {oldest_age_hours:.2f}h >= fail threshold {age_fail}h")
        elif oldest_age_hours >= age_warn and status != FAIL:
            status = WARN
            reasons.append(f"oldest entry {oldest_age_hours:.2f}h >= warn threshold {age_warn}h")

    if count == 0:
        message = "capture queue is empty"
    else:
        message = f"capture queue has {count} queued capture(s)"
        if oldest_age_hours is None:
            message += "; oldest capture age unavailable"
        else:
            message += f"; oldest {oldest_age_hours:.2f}h old"
    if reasons:
        message += f" ({'; '.join(reasons)})"
    return CheckResult("queue health", status, message, details)


def _queue_health_thresholds(config: dict[str, Any]) -> dict[str, int]:
    return {
        "queue_backlog_warn_threshold": _config_int(
            config, "queue_backlog_warn_threshold", _DEFAULT_CONFIG["queue_backlog_warn_threshold"]
        ),
        "queue_backlog_fail_threshold": _config_int(
            config, "queue_backlog_fail_threshold", _DEFAULT_CONFIG["queue_backlog_fail_threshold"]
        ),
        "queue_oldest_age_warn_hours": _config_int(
            config, "queue_oldest_age_warn_hours", _DEFAULT_CONFIG["queue_oldest_age_warn_hours"]
        ),
        "queue_oldest_age_fail_hours": _config_int(
            config, "queue_oldest_age_fail_hours", _DEFAULT_CONFIG["queue_oldest_age_fail_hours"]
        ),
    }


def _pi_queue_file() -> Path:
    from memento.pi_bridge import _state_root

    return _state_root() / "queue" / "pi-captures.jsonl"


def _config_int(config: dict[str, Any], key: str, default: int) -> int:
    try:
        return max(0, int(config.get(key, default)))
    except (TypeError, ValueError):
        return default


def _has_stale_empty_mcp_config(path: Path) -> bool:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    marker = "--mcp-config"
    idx = text.find(marker)
    while idx != -1:
        tail = text[idx : idx + 300]
        if '"{}"' in tail or "'{}'" in tail:
            if '{"mcpServers": {}}' not in tail:
                return True
        idx = text.find(marker, idx + len(marker))
    return False


def _check_triage_health() -> CheckResult:
    cutoff = datetime.now() - timedelta(hours=_HEALTH_WINDOW_HOURS)
    log_path, total, failed, invalid_mcp_failed, stale_certainty_failed, last_error = _scan_triage_logs(cutoff)
    if total == 0:
        return CheckResult(
            "triage", WARN, "no recent triage health events found", {"window_hours": _HEALTH_WINDOW_HOURS}
        )
    failure_threshold_met = telemetry.failure_rate_warns(failed, total)
    if failure_threshold_met:
        message = f"triage failing {failed}/{total} in last {_HEALTH_WINDOW_HOURS}h"
        if invalid_mcp_failed:
            message += f" — {_STALE_MCP_HINT}"
        if stale_certainty_failed:
            message += f" — {_STALE_CERTAINTY_HINT}"
        return CheckResult(
            "triage",
            FAIL if invalid_mcp_failed else WARN,
            message,
            {"log_path": log_path, "failed": failed, "total": total, "last_error": last_error},
        )
    return CheckResult("triage", PASS, f"recent triage health ok ({failed}/{total} failures)", {"log_path": log_path})


def _scan_triage_logs(cutoff: datetime) -> tuple[str | None, int, int, bool, bool, str | None]:
    primary = _scan_triage_log(Path(TRIAGE_HEALTH_LOG_PATH), cutoff, legacy=False)
    if primary[1] >= 3:
        return primary
    legacy = _scan_triage_log(Path(RETRIEVAL_LOG_PATH), cutoff, legacy=True)
    return legacy if legacy[1] >= primary[1] else primary


def _is_stale_certainty_error(error: str) -> bool:
    if "invalid literal for int()" not in error:
        return False
    return any(f"'{label}'" in error or f'"{label}"' in error for label in _ACCEPTED_CERTAINTY_LABELS)


def _scan_triage_log(path: Path, cutoff: datetime, legacy: bool) -> tuple[str | None, int, int, bool, bool, str | None]:
    if not path.exists():
        return (str(path), 0, 0, False, False, None)
    success_actions = {"structured_notes_written"}
    failure_actions = {
        "hook_input_failed",
        "missing_transcript",
        "parse_transcript_failed",
        "structured_notes_failed",
        "structured_notes_llm_failed",
        "structured_notes_lock_timeout",
        "structured_notes_parse_empty",
        "structured_notes_payload_unreadable",
        "structured_notes_transcript_unreadable",
    }
    total = failed = 0
    invalid_mcp_failed = False
    stale_certainty_failed = False
    last_error = None
    for rec in _iter_recent_jsonl(path, cutoff):
        if rec.get("hook") != "triage":
            continue
        action = rec.get("action") or ""
        if legacy:
            if action not in ("decision", "parse_transcript_failed", "structured_notes_llm_failed"):
                continue
            total += 1
            is_failed = action != "decision"
        else:
            if action in success_actions:
                total += 1
                is_failed = False
            elif action in failure_actions:
                total += 1
                is_failed = True
            else:
                continue
        if is_failed:
            failed += 1
            error = str(rec.get("error") or "")
            if error:
                last_error = error
                invalid_mcp_failed = invalid_mcp_failed or _is_invalid_mcp_config_error(error)
                stale_certainty_failed = stale_certainty_failed or _is_stale_certainty_error(error)
    return (str(path), total, failed, invalid_mcp_failed, stale_certainty_failed, last_error)


def _check_retrieval_health(
    *, config: dict[str, Any] | None = None, search_check: CheckResult | None = None
) -> CheckResult:
    path = Path(RETRIEVAL_LOG_PATH)
    cutoff = datetime.now() - timedelta(hours=_HEALTH_WINDOW_HOURS)
    if not path.exists():
        return CheckResult(
            "retrieval", WARN, "retrieval log not found; recall/search failure rate unavailable", {"path": str(path)}
        )

    diagnostics = _scan_retrieval_logs(path, cutoff)
    remediation = _retrieval_remediation(diagnostics, config or {}, search_check)
    details = dict(diagnostics)
    details["log_path"] = str(path)
    details["window_hours"] = _HEALTH_WINDOW_HOURS
    if remediation:
        details["remediation"] = remediation

    total = diagnostics["events"]
    failed = diagnostics["failures"]
    if total == 0:
        if diagnostics["low_signal_skips"]:
            return CheckResult(
                "retrieval",
                PASS,
                f"only low-signal recall/search skips recorded in last {_HEALTH_WINDOW_HOURS}h",
                details,
            )
        return CheckResult("retrieval", PASS, "no recent recall/search failures recorded", details)

    category_summary = (
        f"backend unavailable {diagnostics['backend_unavailable']}, "
        f"backend exceptions {diagnostics['backend_exceptions']}, "
        f"no-results {diagnostics['no_results']}, "
        f"low-signal skips {diagnostics['low_signal_skips']}"
    )
    if diagnostics["failure_rate"] >= telemetry.HEALTH_WARN_FAILURE_RATIO:
        message = (
            f"recall/search backend failures {failed}/{total} in last {_HEALTH_WINDOW_HOURS}h ({category_summary})"
        )
        if remediation:
            message += f"; {remediation[0]}"
        return CheckResult("retrieval", WARN, message, details)

    return CheckResult(
        "retrieval",
        PASS,
        f"recent recall/search health ok ({failed}/{total} failures; {category_summary})",
        details,
    )


def _scan_retrieval_logs(path: Path, cutoff: datetime) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "events": 0,
        "successes": 0,
        "failures": 0,
        "failure_rate": 0.0,
        "no_results": 0,
        "backend_unavailable": 0,
        "backend_exceptions": 0,
        "low_signal_skips": 0,
        "other_failures": 0,
        "last_error": None,
        "last_error_truncated": False,
        "last_error_kind": None,
    }
    for rec in _iter_recent_jsonl(path, cutoff):
        kind = _classify_retrieval_record(rec)
        if kind is None:
            continue
        if kind == "low_signal_skip":
            diagnostics["low_signal_skips"] += 1
            continue

        diagnostics["events"] += 1
        if kind == "success":
            diagnostics["successes"] += 1
        elif kind == "no_result":
            diagnostics["no_results"] += 1
        elif kind == "backend_unavailable":
            diagnostics["backend_unavailable"] += 1
            _record_retrieval_error(diagnostics, rec, kind)
        elif kind == "backend_exception":
            diagnostics["backend_exceptions"] += 1
            _record_retrieval_error(diagnostics, rec, kind)
        else:
            diagnostics["other_failures"] += 1
            _record_retrieval_error(diagnostics, rec, kind)

    diagnostics["failures"] = (
        diagnostics["backend_unavailable"] + diagnostics["backend_exceptions"] + diagnostics["other_failures"]
    )
    diagnostics["failure_rate"] = telemetry.failure_rate(diagnostics["failures"], diagnostics["events"])
    return diagnostics


def _classify_retrieval_record(rec: dict[str, Any]) -> str | None:
    return telemetry.classify_retrieval_record(rec)


def _normalize_retrieval_reason(reason: str) -> str:
    return telemetry.normalize_retrieval_reason(reason)


def _record_retrieval_error(diagnostics: dict[str, Any], rec: dict[str, Any], kind: str) -> None:
    value = rec.get("error") or rec.get("reason") or rec.get("action") or kind
    text, truncated = _safe_retrieval_error(value)
    diagnostics["last_error"] = text
    diagnostics["last_error_truncated"] = truncated
    diagnostics["last_error_kind"] = kind


def _safe_retrieval_error(value: Any) -> tuple[str, bool]:
    return telemetry.safe_error(value, limit=_RETRIEVAL_ERROR_DETAIL_LIMIT)


def _retrieval_remediation(
    diagnostics: dict[str, Any], config: dict[str, Any], search_check: CheckResult | None
) -> list[str]:
    if diagnostics.get("failures", 0) == 0:
        return []
    backend = str(config.get("search_backend") or "auto")
    hints: list[str] = []
    if diagnostics.get("backend_unavailable", 0):
        if backend == "qmd":
            hints.append(
                "qmd search backend is configured but unavailable; verify qmd is on PATH or choose embedded/grep"
            )
        elif backend == "embedded":
            hints.append(
                "embedded search backend is configured but unavailable; run memento_reindex and verify the index path"
            )
        elif backend == "grep":
            hints.append(
                "grep search backend is configured but unavailable; verify the vault notes/projects directories"
            )
        else:
            hints.append(
                "search backend is unavailable; run memento health/status and reindex or reinstall the configured backend"
            )
    if diagnostics.get("backend_exceptions", 0):
        hints.append(
            "search backend raised exceptions; inspect --verbose last_error and run memento_reindex if index state is stale"
        )
    if search_check and search_check.status == FAIL:
        hints.append(search_check.message)
    return list(dict.fromkeys(hints))


def _check_locks() -> CheckResult:
    lock_results = []
    worst = PASS
    messages = []
    for name, paths in (
        ("vault-write", _lock_path_candidates("vault-write.lock")),
        ("inception", _lock_path_candidates("inception.lock")),
    ):
        for path in paths:
            result = _inspect_lock(name, path)
            lock_results.append(result)
            if result["status"] == FAIL:
                worst = FAIL
            elif result["status"] == WARN and worst != FAIL:
                worst = WARN
            if result["message"]:
                messages.append(result["message"])
    message = "; ".join(messages) if messages else "no lock files present"
    return CheckResult("locks", worst, message, {"locks": lock_results})


def _lock_path_candidates(filename: str) -> list[Path]:
    configured = Path(VAULT_WRITE_LOCK_PATH if filename == "vault-write.lock" else INCEPTION_LOCK_PATH)
    candidates = [
        configured,
        Path(os.environ.get("XDG_RUNTIME_DIR", Path.home() / ".cache")) / "memento-vault" / filename,
        Path.home() / ".cache" / "memento-vault" / filename,
        Path(os.environ.get("TMPDIR", "/tmp")) / f"memento-vault-{os.getuid()}" / filename,
    ]
    unique = []
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _inspect_lock(name: str, path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"name": name, "path": str(path), "status": PASS, "message": ""}
    try:
        age = max(0, time.time() - path.stat().st_mtime)
        pid_text = path.read_text(errors="replace").strip()
        pid = int(pid_text) if pid_text else None
    except (OSError, ValueError):
        return {"name": name, "path": str(path), "status": WARN, "message": f"{name} lock is unreadable"}
    live = _pid_is_live(pid) if pid else False
    if live and age >= _STALE_LOCK_SECONDS:
        return {
            "name": name,
            "path": str(path),
            "pid": pid,
            "age_seconds": int(age),
            "status": FAIL,
            "message": f"{name} lock held by long-running live pid {pid}",
        }
    if live:
        return {
            "name": name,
            "path": str(path),
            "pid": pid,
            "age_seconds": int(age),
            "status": WARN,
            "message": f"{name} lock currently held by pid {pid}",
        }
    return {
        "name": name,
        "path": str(path),
        "pid": pid,
        "age_seconds": int(age),
        "status": WARN,
        "message": f"{name} lock appears stale",
    }


def _pid_is_live(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM


def _missing_inception_dependencies() -> list[str]:
    return [pkg for pkg in ("numpy", "hdbscan", "sklearn") if importlib.util.find_spec(pkg) is None]


def _safe_excerpt(value: Any, limit: int = _INCEPTION_ERROR_DETAIL_LIMIT) -> tuple[str, bool]:
    text = _sanitize_secrets(" ".join(str(value or "").split()))
    truncated = len(text) > limit
    if truncated:
        text = text[:limit] + "..."
    return text, truncated


def _format_duration(seconds: int) -> str:
    remaining = max(0, int(seconds))
    parts: list[str] = []
    for suffix, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if remaining >= size:
            value, remaining = divmod(remaining, size)
            if value:
                parts.append(f"{value}{suffix}")
    if remaining or not parts:
        parts.append(f"{remaining}s")
    return " ".join(parts)


def _summarize_inception_runs(runs: Any, limit: int = _INCEPTION_RECENT_RUNS_LIMIT) -> list[dict[str, Any]]:
    if not isinstance(runs, list):
        return []
    summaries: list[dict[str, Any]] = []
    for run in runs[-limit:]:
        if not isinstance(run, dict):
            summaries.append(_sanitize_obj(run))
            continue
        item: dict[str, Any] = {}
        for key in ("iso", "clusters_found", "notes_written", "dry_run"):
            if key in run:
                item[key] = run[key]
        for key in ("error", "last_error", "reason"):
            if run.get(key):
                excerpt, truncated = _safe_excerpt(run[key])
                item["error"] = excerpt
                item["error_truncated"] = truncated
                break
        summaries.append(_sanitize_obj(item))
    return summaries


def _extract_inception_error(state: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[tuple[str, Any]] = []
    for key in ("last_error", "error"):
        value = state.get(key)
        if value:
            candidates.append((key, value))
    for key in ("last_failure", "failure"):
        value = state.get(key)
        if isinstance(value, dict):
            for subkey in ("error", "message", "reason"):
                if value.get(subkey):
                    candidates.append((f"{key}.{subkey}", value[subkey]))
                    break
        elif value:
            candidates.append((key, value))
    runs = state.get("runs")
    if isinstance(runs, list):
        for index, run in enumerate(reversed(runs), start=1):
            if not isinstance(run, dict):
                continue
            for key in ("last_error", "error", "exception", "reason"):
                value = run.get(key)
                if value:
                    candidates.append((f"runs[-{index}].{key}", value))
                    break
    for source, value in candidates:
        excerpt, truncated = _safe_excerpt(value)
        if excerpt:
            return {"source": source, "error": excerpt, "truncated": truncated}
    return None


def _last_inception_run(state: dict[str, Any]) -> tuple[datetime | None, str | None, str | None]:
    raw = state.get("last_run_iso")
    dt = _parse_ts(raw)
    if dt is not None:
        return dt, str(raw), "last_run_iso"
    runs = state.get("runs")
    if isinstance(runs, list):
        for index, run in enumerate(reversed(runs), start=1):
            if not isinstance(run, dict):
                continue
            for key in ("iso", "ts", "last_run_iso"):
                raw_run = run.get(key)
                dt = _parse_ts(raw_run)
                if dt is not None:
                    return dt, dt.isoformat(timespec="seconds"), f"runs[-{index}].{key}"
    return None, None, None


def _check_inception(config: dict[str, Any]) -> CheckResult:
    if not config.get("inception_enabled", False):
        return CheckResult("inception", PASS, "inception disabled")

    state_path = Path(INCEPTION_STATE_PATH)
    details: dict[str, Any] = {
        "state_path": str(state_path),
        "lock_path": str(INCEPTION_LOCK_PATH),
        "state_present": state_path.exists(),
    }
    issue_messages: list[str] = []
    summary_messages: list[str] = []
    status = PASS

    missing_dependencies = _missing_inception_dependencies()
    if missing_dependencies:
        details["missing_dependencies"] = missing_dependencies
        issue_messages.append(f"missing optional dependencies: {', '.join(missing_dependencies)}")
        status = FAIL

    lock_details = _inspect_lock("inception", Path(INCEPTION_LOCK_PATH))
    details["lock"] = _sanitize_obj(lock_details)
    if lock_details["message"]:
        issue_messages.append(lock_details["message"])
    if lock_details["status"] == FAIL:
        status = FAIL
    elif lock_details["status"] == WARN and status == PASS:
        status = WARN

    if not state_path.exists():
        issue_messages.append("state file is missing")
        if status == PASS:
            status = WARN
        message = (
            "; ".join(issue_messages)
            if issue_messages
            else f"inception enabled but state file is missing: {state_path}"
        )
        return CheckResult("inception", status, message, details)

    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        details["state_error"] = _safe_text(str(exc))
        issue_messages.append(f"state file is unreadable: {exc}")
        if status == PASS:
            status = WARN
        message = "; ".join(issue_messages)
        return CheckResult("inception", status, message, details)

    if not isinstance(state, dict):
        issue_messages.append("state file root must be an object")
        if status == PASS:
            status = WARN
        message = "; ".join(issue_messages)
        return CheckResult("inception", status, message, details)

    details["state_valid"] = True
    runs = state.get("runs") if isinstance(state.get("runs"), list) else []
    details["run_count"] = len(runs)
    details["processed_notes_count"] = (
        len(state.get("processed_notes", [])) if isinstance(state.get("processed_notes"), list) else 0
    )
    details["last_run_note_count"] = state.get("last_run_note_count")
    details["recent_runs"] = _summarize_inception_runs(runs)

    last_run_dt, last_run_iso, last_run_source = _last_inception_run(state)
    if last_run_dt is None:
        issue_messages.append("no recorded run yet")
        if status == PASS:
            status = WARN
    else:
        age_seconds = int(max(0, (datetime.now() - last_run_dt).total_seconds()))
        details["last_run_iso"] = last_run_iso
        details["last_run_source"] = last_run_source
        details["last_run_age_seconds"] = age_seconds
        details["last_run_age_human"] = _format_duration(age_seconds)
        if state.get("last_run_iso") is None or last_run_source != "last_run_iso":
            issue_messages.append("last_run_iso missing or invalid; using most recent run summary")
            if status == PASS:
                status = WARN
        summary_messages.append(f"last ran {details['last_run_age_human']} ago")
        summary_messages.append(f"{details['run_count']} recorded run(s)")
        last_count = details["last_run_note_count"]
        if last_count is not None:
            summary_messages.append(f"last run note count {last_count}")

    last_error = _extract_inception_error(state)
    if last_error:
        details["last_error"] = last_error["error"]
        details["last_error_source"] = last_error["source"]
        details["last_error_truncated"] = last_error["truncated"]
        summary_messages.append(f'last error: "{last_error["error"]}"')

    message_parts = issue_messages + summary_messages
    message = "; ".join(message_parts) if message_parts else "inception enabled"
    return CheckResult("inception", status, message, details)


def _iter_jsonl(path: Path):
    yield from telemetry.iter_jsonl(path)


def _iter_recent_jsonl(path: Path, cutoff: datetime):
    yield from telemetry.iter_recent_jsonl(path, cutoff)


def _parse_ts(raw: Any) -> datetime | None:
    return telemetry.parse_timestamp_naive_utc(raw)


def _is_invalid_mcp_config_error(message: str) -> bool:
    normalized = (message or "").lower()
    return "invalid mcp configuration" in normalized or ("mcpservers" in normalized and "schema" in normalized)


def _sanitize_secrets(text: str) -> str:
    return telemetry.redact_text(text)


def _safe_text(text: str) -> str:
    return telemetry.safe_text(text)


def _sanitize_obj(value: Any) -> Any:
    return telemetry.sanitize_obj(value)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
