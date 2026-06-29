"""Read-only operational health diagnostics for memento-vault."""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import subprocess
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
    manifest_check, manifest = _check_install_manifest()
    checks.append(manifest_check)
    checks.append(_check_managed_files(manifest))
    checks.append(_check_claude_hooks(manifest))
    checks.extend(_check_mcp_config())
    checks.append(_check_mcp_registration())
    checks.append(_check_pi_bridge_config())
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
    options = (
        manifest.get("options") if isinstance(manifest, dict) and isinstance(manifest.get("options"), dict) else {}
    )
    expected = [("SessionEnd", "memento-triage.py")]
    if options.get("experimental"):
        expected.extend(
            [
                ("SessionStart", "vault-briefing.py"),
                ("UserPromptSubmit", "vault-recall.py"),
                ("PreToolUse", "vault-tool-context.py"),
            ]
        )
    return expected


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
            completed = subprocess.run(
                [client, "mcp", "get", "memento-vault"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=3,
                check=False,
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


_PI_BOOL_KEYS = {"enabled", "briefing", "promptRecall", "toolContext", "autoCapture", "captureQueue"}
_PI_INT_KEYS = {"maxInjectedChars", "maxToolContextPerSession"}


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
    if invalid:
        return CheckResult(
            "pi bridge",
            WARN,
            f"Pi bridge config has invalid key types: {', '.join(invalid)}",
            {"path": str(path), "invalid_keys": invalid},
        )
    configured = sorted(key for key in candidate if key in _PI_BOOL_KEYS or key in _PI_INT_KEYS)
    return CheckResult(
        "pi bridge", PASS, "Pi bridge config shape looks valid", {"path": str(path), "configured_keys": configured}
    )


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
    failure_threshold_met = total >= 3 and failed / total >= 0.5
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
