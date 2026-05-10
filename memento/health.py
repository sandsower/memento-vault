"""Read-only operational health diagnostics for memento-vault."""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PASS = "pass"
WARN = "warn"
FAIL = "fail"
_STATUSES = (PASS, WARN, FAIL)
_EXPECTED_DIRS = ("notes", "fleeting", "projects", "archive")
_CORE_DIRS = ("notes", "fleeting", "projects")
_HEALTH_WINDOW_HOURS = 24
_STALE_LOCK_SECONDS = 600
_RECENT_FAILURE_ACTION_MARKERS = ("failed", "failure", "error", "unexpected", "unavailable")
_STALE_MCP_HINT = (
    "likely stale headless Claude MCP config; rerun ./install.sh --reinstall; "
    'copied hooks should use {"mcpServers": {}} for --mcp-config'
)
_DEFAULT_CONFIG = {
    "vault_path": str(Path.home() / "memento"),
    "auto_commit": True,
    "search_backend": "auto",
    "search_db_path": ".search/search.db",
    "inception_enabled": False,
}
RETRIEVAL_LOG_PATH = str(
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "memento-vault" / "retrieval.jsonl"
)
TRIAGE_HEALTH_LOG_PATH = str(
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "memento-vault" / "triage-health.jsonl"
)
INCEPTION_STATE_PATH = str(
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "memento-vault" / "inception-state.json"
)
_DEFAULT_RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", Path.home() / ".cache")) / "memento-vault"
VAULT_WRITE_LOCK_PATH = str(_DEFAULT_RUNTIME_DIR / "vault-write.lock")
INCEPTION_LOCK_PATH = str(_DEFAULT_RUNTIME_DIR / "inception.lock")

_SECRET_PATTERNS = [
    (r"(sk-[a-zA-Z0-9]{20,})", "[REDACTED_API_KEY]"),
    (r"(sk-proj-[a-zA-Z0-9_-]{20,})", "[REDACTED_API_KEY]"),
    (r"(ghp_[a-zA-Z0-9]{36,})", "[REDACTED_GITHUB_TOKEN]"),
    (r"(gho_[a-zA-Z0-9]{36,})", "[REDACTED_GITHUB_TOKEN]"),
    (r"(github_pat_[a-zA-Z0-9_]{20,})", "[REDACTED_GITHUB_TOKEN]"),
    (r"(xox[bp]-[a-zA-Z0-9\-]+)", "[REDACTED_SLACK_TOKEN]"),
    (r"(AKIA[0-9A-Z]{16})", "[REDACTED_AWS_KEY]"),
    (r"(eyJ[a-zA-Z0-9_\-]{10,}\.eyJ[a-zA-Z0-9_\-]{10,})", "[REDACTED_JWT]"),
    (r'((?:postgres|mysql|mongodb|redis)://[^\s"\'`]+)', "[REDACTED_CONNECTION_STRING]"),
    (r"(Bearer\s+[a-zA-Z0-9_\-.]{20,})", "Bearer [REDACTED_TOKEN]"),
    (r'(?:_KEY|_SECRET|_TOKEN|_PASSWORD|_PASS)\s*[=:]\s*["\']?([a-zA-Z0-9_\-/.]{20,})["\']?', "[REDACTED_SECRET]"),
]


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

    def to_dict(self, verbose: bool = True) -> dict[str, Any]:
        checks = []
        for check in self.checks:
            item = asdict(check)
            if not verbose:
                item.pop("details", None)
            checks.append(item)
        return {"status": self.status, "summary": dict(self.summary), "checks": checks}


def build_report() -> HealthReport:
    """Run cheap, read-only health checks."""
    checks: list[CheckResult] = []
    config_check, config = _check_config_parse()
    checks.append(config_check)

    vault = Path(config.get("vault_path") or _DEFAULT_CONFIG["vault_path"]).expanduser()
    checks.append(_check_vault_dirs(vault))
    checks.append(_check_git(vault, config))
    checks.append(_check_search_backend(vault, config))
    checks.extend(_check_mcp_config())
    checks.append(_check_triage_health())
    checks.append(_check_retrieval_health())
    checks.append(_check_locks())
    checks.append(_check_inception(config))

    summary = {status: sum(1 for check in checks if check.status == status) for status in _STATUSES}
    status = FAIL if summary[FAIL] else WARN if summary[WARN] else PASS
    return HealthReport(status=status, summary=summary, checks=checks)


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
    args = parser.parse_args(argv)

    report = build_report()
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
            elif value.startswith("[") and value.endswith("]"):
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


def _check_mcp_config() -> list[CheckResult]:
    checks = []
    config_path = Path.home() / ".claude" / "mcp-servers.json"
    if config_path.exists():
        try:
            json.loads(config_path.read_text())
            checks.append(CheckResult("mcp config", PASS, f"valid JSON at {config_path}", {"path": str(config_path)}))
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
        checks.append(CheckResult("mcp config", WARN, "Claude MCP config not found", {"path": str(config_path)}))

    stale_paths = []
    for path in (Path.home() / ".claude" / "hooks" / "memento" / "llm.py", Path(__file__).with_name("llm.py")):
        if _has_stale_empty_mcp_config(path):
            stale_paths.append(str(path))
    if stale_paths:
        checks.append(CheckResult("headless claude mcp", WARN, _STALE_MCP_HINT, {"paths": stale_paths}))
    else:
        checks.append(CheckResult("headless claude mcp", PASS, "headless Claude empty MCP config shape looks current"))
    return checks


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
    log_path, total, failed, invalid_mcp_failed, last_error = _scan_triage_logs(cutoff)
    if total == 0:
        return CheckResult(
            "triage", WARN, "no recent triage health events found", {"window_hours": _HEALTH_WINDOW_HOURS}
        )
    failure_threshold_met = total >= 3 and failed / total >= 0.5
    if failure_threshold_met and invalid_mcp_failed:
        return CheckResult(
            "triage",
            FAIL,
            f"triage failing {failed}/{total} in last {_HEALTH_WINDOW_HOURS}h — {_STALE_MCP_HINT}",
            {"log_path": log_path, "failed": failed, "total": total, "last_error": last_error},
        )
    if failure_threshold_met:
        return CheckResult(
            "triage",
            WARN,
            f"triage failing {failed}/{total} in last {_HEALTH_WINDOW_HOURS}h",
            {"log_path": log_path, "failed": failed, "total": total, "last_error": last_error},
        )
    return CheckResult("triage", PASS, f"recent triage health ok ({failed}/{total} failures)", {"log_path": log_path})


def _scan_triage_logs(cutoff: datetime) -> tuple[str | None, int, int, bool, str | None]:
    primary = _scan_triage_log(Path(TRIAGE_HEALTH_LOG_PATH), cutoff, legacy=False)
    if primary[1] >= 3:
        return primary
    legacy = _scan_triage_log(Path(RETRIEVAL_LOG_PATH), cutoff, legacy=True)
    return legacy if legacy[1] >= primary[1] else primary


def _scan_triage_log(path: Path, cutoff: datetime, legacy: bool) -> tuple[str | None, int, int, bool, str | None]:
    if not path.exists():
        return (str(path), 0, 0, False, None)
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
    return (str(path), total, failed, invalid_mcp_failed, last_error)


def _check_retrieval_health() -> CheckResult:
    path = Path(RETRIEVAL_LOG_PATH)
    cutoff = datetime.now() - timedelta(hours=_HEALTH_WINDOW_HOURS)
    if not path.exists():
        return CheckResult(
            "retrieval", WARN, "retrieval log not found; recall/search failure rate unavailable", {"path": str(path)}
        )
    total = failed = 0
    last_error = None
    for rec in _iter_recent_jsonl(path, cutoff):
        hook = rec.get("hook")
        if hook not in {"recall", "search"}:
            continue
        action = str(rec.get("action") or "")
        if action in {"low-signal-prompt", "skipped-prompt", "deferred-ready"}:
            continue
        total += 1
        if any(marker in action for marker in _RECENT_FAILURE_ACTION_MARKERS):
            failed += 1
            last_error = rec.get("error") or action
    if total == 0:
        return CheckResult("retrieval", PASS, "no recent recall/search failures recorded", {"log_path": str(path)})
    if failed / total >= 0.5:
        return CheckResult(
            "retrieval",
            WARN,
            f"recall/search failures {failed}/{total} in last {_HEALTH_WINDOW_HOURS}h",
            {"log_path": str(path), "last_error": last_error},
        )
    return CheckResult(
        "retrieval", PASS, f"recent recall/search health ok ({failed}/{total} failures)", {"log_path": str(path)}
    )


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


def _check_inception(config: dict[str, Any]) -> CheckResult:
    if not config.get("inception_enabled", False):
        return CheckResult("inception", PASS, "inception disabled")
    state_path = Path(INCEPTION_STATE_PATH)
    if not state_path.exists():
        return CheckResult(
            "inception", WARN, "inception enabled but state file is missing", {"state_path": str(state_path)}
        )
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("inception", WARN, f"inception state is unreadable: {exc}", {"state_path": str(state_path)})
    last_run = state.get("last_run_iso")
    if not last_run:
        return CheckResult(
            "inception", WARN, "inception enabled but has no recorded run", {"state_path": str(state_path)}
        )
    return CheckResult(
        "inception",
        PASS,
        f"inception last ran at {last_run}",
        {"state_path": str(state_path), "last_run_note_count": state.get("last_run_note_count")},
    )


def _iter_recent_jsonl(path: Path, cutoff: datetime):
    try:
        with path.open() as handle:
            for line in handle:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(rec.get("ts"))
                if ts is None or ts < cutoff:
                    continue
                yield rec
    except OSError:
        return


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def _is_invalid_mcp_config_error(message: str) -> bool:
    normalized = (message or "").lower()
    return "invalid mcp configuration" in normalized or ("mcpservers" in normalized and "schema" in normalized)


def _sanitize_secrets(text: str) -> str:
    import re

    if not text:
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def _safe_text(text: str) -> str:
    text = _sanitize_secrets(str(text))
    if len(text) > 1000:
        return text[:1000] + "..."
    return text


def _sanitize_obj(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _sanitize_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_obj(v) for v in value]
    if isinstance(value, tuple):
        return [_sanitize_obj(v) for v in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
