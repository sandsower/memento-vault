#!/usr/bin/env python3
"""Fast release/install/package smoke checks for memento-vault.

The default path is intentionally safe for local gates and CI: it only reads
repository files and runs help/version commands that must not mutate the
machine. Heavier tool-backed checks are opt-in with ``--heavy`` and skip with
clear messages when optional tools are unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
INIT_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')
FORMULA_URL_VERSION_RE = re.compile(r"/refs/tags/v([^/]+)\.tar\.gz")


def pass_(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "PASS", detail)


def fail(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "FAIL", detail)


def skip(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "SKIP", detail)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require_file(root: Path, relative: str) -> tuple[Path | None, CheckResult | None]:
    path = root / relative
    if not path.exists():
        return None, fail("required file", f"Missing {relative}; restore it before release.")
    return path, None


def check_version_consistency(root: Path) -> CheckResult:
    expected_path = root / "VERSION"
    if not expected_path.exists():
        return fail("version consistency", "Missing VERSION; restore VERSION before release.")

    expected = read_text(expected_path).strip()
    if not VERSION_RE.match(expected):
        return fail("version consistency", f"VERSION has {expected!r}; expected semantic version like 4.1.0.")

    mismatches: list[str] = []

    package_path = root / "package.json"
    if package_path.exists():
        try:
            package_version = json.loads(read_text(package_path))["version"]
        except (json.JSONDecodeError, KeyError) as exc:
            mismatches.append(f"package.json could not be parsed for version: {exc}")
        else:
            if package_version != expected:
                mismatches.append(f"package.json has {package_version}, expected {expected}. Update package.json.")
    else:
        mismatches.append("package.json is missing. Restore package.json.")

    init_path = root / "memento" / "__init__.py"
    if init_path.exists():
        match = INIT_VERSION_RE.search(read_text(init_path))
        if not match:
            mismatches.append("memento/__init__.py has no __version__. Add/update __version__.")
        elif match.group(1) != expected:
            mismatches.append(f"memento/__init__.py has {match.group(1)}, expected {expected}. Update __version__.")
    else:
        mismatches.append("memento/__init__.py is missing. Restore it before release.")

    formula_path = root / "Formula" / "memento-vault.rb"
    if formula_path.exists():
        match = FORMULA_URL_VERSION_RE.search(read_text(formula_path))
        if not match:
            mismatches.append("Formula/memento-vault.rb has no vX.Y.Z release URL. Update the formula URL.")
        elif match.group(1) != expected:
            mismatches.append(
                f"Formula/memento-vault.rb URL has {match.group(1)}, expected {expected}. Update formula URL."
            )
    else:
        mismatches.append("Formula/memento-vault.rb is missing. Restore the Homebrew formula.")

    if mismatches:
        return fail("version consistency", " ".join(mismatches))
    return pass_(
        "version consistency", f"VERSION, package.json, memento/__init__.py, and Formula URL agree on {expected}."
    )


def check_homebrew_formula(root: Path) -> CheckResult:
    formula = root / "Formula" / "memento-vault.rb"
    if not formula.exists():
        return fail("homebrew formula", "Missing Formula/memento-vault.rb; restore formula before release.")

    text = read_text(formula)
    required_snippets = [
        "class MementoVault < Formula",
        'homepage "https://github.com/sandsower/memento-vault"',
        'depends_on "git"',
        'depends_on "python@3"',
        'bin.install_symlink libexec/"bin/memento-vault"',
        "test do",
        "memento-vault version",
    ]
    missing = [snippet for snippet in required_snippets if snippet not in text]
    if missing:
        return fail("homebrew formula", f"Missing expected formula snippets: {', '.join(missing)}.")
    if "UPDATE_WITH_ACTUAL_SHA256_AFTER_RELEASE" not in text and "sha256" not in text:
        return fail(
            "homebrew formula", "Formula has no sha256 placeholder or checksum; update release checksum handling."
        )
    return pass_(
        "homebrew formula", "Static formula checks passed; run brew test in release environment when available."
    )


def check_pi_package_metadata(root: Path) -> CheckResult:
    package_path = root / "package.json"
    if not package_path.exists():
        return fail("pi package metadata", "Missing package.json; restore pi package metadata.")
    try:
        package = json.loads(read_text(package_path))
    except json.JSONDecodeError as exc:
        return fail("pi package metadata", f"package.json is invalid JSON: {exc}.")

    problems: list[str] = []
    if package.get("private") is not True:
        problems.append("package.json should remain private for local pi package metadata.")
    if "pi-package" not in package.get("keywords", []):
        problems.append("package.json keywords should include pi-package.")
    pi_config = package.get("pi") or {}
    if "./extensions/memento.ts" not in pi_config.get("extensions", []):
        problems.append("package.json pi.extensions must include ./extensions/memento.ts.")
    if "skills/generic" not in pi_config.get("skills", []):
        problems.append("package.json pi.skills must include skills/generic.")
    if not package.get("files"):
        problems.append("package.json files list is empty; package dry-runs would omit release assets.")

    if problems:
        return fail("pi package metadata", " ".join(problems))
    return pass_("pi package metadata", "package.json exposes pi extension and skills metadata.")


def check_shell_syntax(root: Path) -> CheckResult:
    """Parse every tracked shell script with bash -n.

    On macOS /bin/bash is 3.2, the oldest bash users install with (Homebrew
    formulae run install scripts through it) — this is the gate that would
    have caught GH #90, where 4.x-only syntax shipped in a release.
    """
    try:
        listing = subprocess.run(["git", "ls-files", "*.sh"], cwd=root, text=True, capture_output=True, timeout=20)
        scripts = [line for line in listing.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return fail("shell syntax", f"Could not list shell scripts: {exc}")
    if not scripts:
        return skip("shell syntax", "No tracked .sh files found.")

    bash = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")
    if not bash:
        return skip("shell syntax", "bash not found; cannot parse shell scripts.")

    version = subprocess.run([bash, "-c", "echo $BASH_VERSION"], text=True, capture_output=True).stdout.strip()
    failures = []
    for script in scripts:
        result = subprocess.run([bash, "-n", script], cwd=root, text=True, capture_output=True, timeout=20)
        if result.returncode != 0:
            failures.append(f"{script}: {(result.stderr or result.stdout).strip()[:200]}")
    if failures:
        return fail("shell syntax", f"bash {version} rejected: " + " | ".join(failures))
    return pass_("shell syntax", f"{len(scripts)} scripts parse under bash {version}.")


def check_install_execution(root: Path) -> CheckResult:
    """Run install.sh non-interactively against a throwaway HOME.

    Exercises the curl|bash shape (stdin not a tty): the install must finish
    without prompting and produce the hook layout. Only the temp HOME is
    mutated.
    """
    installer = root / "install.sh"
    if not installer.exists():
        return fail("install execution", "Missing install.sh.")

    home = Path(tempfile.mkdtemp(prefix="memento-install-smoke-"))
    try:
        (home / ".gitconfig").write_text("[user]\n\temail = smoke@invalid\n\tname = Install Smoke\n")
        env = os.environ.copy()
        env["HOME"] = str(home)
        # Keep XDG state inside the sandbox even if the caller overrides it.
        for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "MEMENTO_VAULT_PATH"):
            env.pop(key, None)
        result = subprocess.run(
            ["bash", str(installer)],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=300,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            return fail("install execution", f"install.sh exited {result.returncode}. Output tail: {output[-600:]}")
        problems = []
        if not (home / ".claude" / "hooks" / "memento-triage.py").exists():
            problems.append("missing ~/.claude/hooks/memento-triage.py")
        if not (home / ".claude" / "hooks" / "memento" / "lifecycle.py").exists():
            problems.append("missing ~/.claude/hooks/memento/lifecycle.py")
        settings = home / ".claude" / "settings.json"
        if not settings.exists():
            problems.append("missing ~/.claude/settings.json")
        else:
            try:
                hooks = json.loads(settings.read_text()).get("hooks", {})
            except json.JSONDecodeError:
                hooks = {}
            if "SessionEnd" not in hooks:
                problems.append("SessionEnd hook not registered in settings.json")
        if not (home / "memento" / "notes").is_dir():
            problems.append("vault notes/ not created")
        if problems:
            return fail("install execution", "; ".join(problems))
        return pass_("install execution", "Non-interactive fresh-HOME install completed with expected layout.")
    except subprocess.TimeoutExpired:
        return fail("install execution", "install.sh timed out after 300s (likely an unguarded prompt).")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def run_command(name: str, command: list[str], *, root: Path, expect_stdout: str | None = None) -> CheckResult:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{root}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(root)
    try:
        result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=20)
    except FileNotFoundError as exc:
        return fail(name, f"Command not found: {command[0]} ({exc}).")
    except subprocess.TimeoutExpired:
        return fail(name, f"Command timed out: {' '.join(command)}.")

    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return fail(name, f"Command failed ({result.returncode}): {' '.join(command)}. Output: {output[:600]}")
    if expect_stdout and expect_stdout not in output:
        return fail(name, f"Expected {expect_stdout!r} in output from {' '.join(command)}. Output: {output[:600]}")
    return pass_(name, f"{' '.join(command)}")


def safe_command_checks(root: Path) -> Iterable[CheckResult]:
    cli = root / "bin" / "memento-vault"
    if not cli.exists():
        yield fail("cli help", "Missing bin/memento-vault; restore CLI wrapper.")
        yield fail("cli version", "Missing bin/memento-vault; restore CLI wrapper.")
    else:
        yield run_command("cli help", [str(cli), "help"], root=root, expect_stdout="Usage: memento-vault")
        expected_version = read_text(root / "VERSION").strip() if (root / "VERSION").exists() else None
        yield run_command("cli version", [str(cli), "version"], root=root, expect_stdout=expected_version)

    installer = root / "install.sh"
    if installer.exists():
        yield run_command(
            "install help", [str(installer), "--help"], root=root, expect_stdout="Memento Vault installer"
        )
    else:
        yield fail("install help", "Missing install.sh; restore installer before release.")

    yield run_command(
        "python module help",
        [sys.executable, "-m", "memento", "--help"],
        root=root,
        expect_stdout="Memento Vault MCP Server",
    )


def heavy_checks(root: Path) -> Iterable[CheckResult]:
    docker = shutil.which("docker")
    if not docker:
        yield skip("docker compose config", "docker not found; install Docker to run compose validation.")
    else:
        compose = root / "docker-compose.yml"
        if compose.exists():
            yield run_command("docker compose config", [docker, "compose", "-f", str(compose), "config"], root=root)
        else:
            yield skip("docker compose config", "docker-compose.yml not present.")

    npm = shutil.which("npm")
    if not npm:
        yield skip("npm package dry-run", "npm not found; install npm to run package dry-run.")
    else:
        yield run_command("npm package dry-run", [npm, "pack", "--dry-run", "--ignore-scripts"], root=root)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run safe release/install/package smoke checks.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root to check (default: parent of this script).",
    )
    parser.add_argument(
        "--skip-command-checks",
        action="store_true",
        help="Only run static metadata checks; useful for minimal fixture repos.",
    )
    parser.add_argument(
        "--heavy",
        action="store_true",
        help="Also run optional tool-backed checks such as Docker Compose and npm pack dry-runs.",
    )
    parser.add_argument(
        "--install-exec",
        action="store_true",
        help="Run install.sh non-interactively against a throwaway HOME (slower; mutates only the temp HOME).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.repo_root.resolve()

    checks: list[CheckResult] = [
        check_version_consistency(root),
        check_homebrew_formula(root),
        check_pi_package_metadata(root),
        check_shell_syntax(root),
    ]
    if args.skip_command_checks:
        checks.extend(
            [
                skip("cli help", "--skip-command-checks requested."),
                skip("cli version", "--skip-command-checks requested."),
                skip("install help", "--skip-command-checks requested."),
                skip("python module help", "--skip-command-checks requested."),
            ]
        )
    else:
        checks.extend(safe_command_checks(root))

    if args.heavy:
        checks.extend(heavy_checks(root))

    if args.install_exec:
        checks.append(check_install_execution(root))

    for result in checks:
        print(f"{result.status} {result.name}: {result.detail}")

    failures = [result for result in checks if result.status == "FAIL"]
    if failures:
        print(f"release smoke failed: {len(failures)} failure(s)")
        return 1

    skipped = sum(1 for result in checks if result.status == "SKIP")
    suffix = f" ({skipped} skipped)" if skipped else ""
    print(f"release smoke passed{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
