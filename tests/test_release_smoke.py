import json
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release_smoke.py"


def run_smoke(*args, cwd=REPO_ROOT):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=30,
    )


def copy_release_metadata(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "bin").mkdir(parents=True)
    (root / "memento").mkdir()
    (root / "Formula").mkdir()
    shutil.copy(REPO_ROOT / "VERSION", root / "VERSION")
    shutil.copy(REPO_ROOT / "package.json", root / "package.json")
    shutil.copy(REPO_ROOT / "memento" / "__init__.py", root / "memento" / "__init__.py")
    shutil.copy(REPO_ROOT / "Formula" / "memento-vault.rb", root / "Formula" / "memento-vault.rb")
    return root


def test_release_smoke_default_safe_subset_passes():
    result = run_smoke()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "release smoke passed" in result.stdout
    assert "PASS version consistency" in result.stdout
    assert "PASS cli help" in result.stdout
    assert "PASS python module help" in result.stdout


def test_release_smoke_reports_actionable_version_mismatch(tmp_path):
    root = copy_release_metadata(tmp_path)
    package = json.loads((root / "package.json").read_text())
    package["version"] = "9.9.9"
    (root / "package.json").write_text(json.dumps(package))

    result = run_smoke("--repo-root", str(root), "--skip-command-checks")

    assert result.returncode == 1
    assert "FAIL version consistency" in result.stdout
    assert "package.json has 9.9.9, expected" in result.stdout
    assert "Update package.json" in result.stdout


def test_release_smoke_optional_heavy_checks_skip_when_tools_are_missing(tmp_path):
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()
    env_path = empty_bin.as_posix()

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--heavy", "--skip-command-checks"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        env={"PATH": env_path},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP docker compose config" in result.stdout
    assert "SKIP npm package dry-run" in result.stdout


def test_release_smoke_can_skip_command_checks_for_metadata_only_tmp_repo(tmp_path):
    root = copy_release_metadata(tmp_path)

    result = run_smoke("--repo-root", str(root), "--skip-command-checks")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS version consistency" in result.stdout
    assert "PASS homebrew formula" in result.stdout
    assert "PASS pi package metadata" in result.stdout
    assert "SKIP cli help" in result.stdout
