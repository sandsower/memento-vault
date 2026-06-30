import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "vault-health-check.sh"


def _make_vault(path: Path) -> Path:
    for dirname in ("fleeting", "notes", "projects", "archive"):
        (path / dirname).mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


def _run_health_check(vault: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(vault)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_legacy_vault_health_check_keeps_direct_callers_and_points_to_health_command(tmp_path):
    vault = _make_vault(tmp_path / "vault")
    (vault / "notes" / "linked.md").write_text("---\ntitle: linked\n---\n")
    (vault / "notes" / "source.md").write_text("---\ntitle: source\n---\n\nSee [[linked]].\n")

    result = _run_health_check(vault)

    assert result.returncode == 0
    assert "Legacy structural vault check" in result.stdout
    assert "memento-vault health" in result.stdout
    assert "Vault health check passed." in result.stdout


def test_legacy_vault_health_check_still_reports_structural_issues(tmp_path):
    vault = _make_vault(tmp_path / "vault")
    (vault / ".git").rmdir()
    (vault / "notes" / "Bad Note.md").write_text("no frontmatter\n\nSee [[missing]].\n")

    result = _run_health_check(vault)

    assert result.returncode == 1
    assert "NO FRONTMATTER" in result.stdout
    assert "BROKEN LINK: [[missing]]" in result.stdout
    assert "NAMING: Bad Note.md" in result.stdout
    assert "WARNING: Vault is not a git repository" in result.stdout
    assert "Found 4 issue(s)." in result.stdout
