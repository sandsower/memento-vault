from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    return result


def _git_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "GIT_AUTHOR_NAME": "Memento Test",
            "GIT_AUTHOR_EMAIL": "memento-test@example.invalid",
            "GIT_COMMITTER_NAME": "Memento Test",
            "GIT_COMMITTER_EMAIL": "memento-test@example.invalid",
        }
    )
    return env


def _write_home_config(home: Path, vault: Path, extra: str = "") -> None:
    config_dir = home / ".config" / "memento-vault"
    config_dir.mkdir(parents=True)
    config_dir.joinpath("memento.yml").write_text(f'vault_path: "{vault}"\n{extra}', encoding="utf-8")


def test_node_extension_harnesses_run_under_node_test() -> None:
    node = shutil.which("node")
    if not node:
        pytest.fail("node is required for the memento extension harness")
    result = subprocess.run(
        [node, "--test", str(REPO_ROOT / "tests" / "node" / "memento-extension.test.mjs")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_vault_commit_records_deleted_note_tombstones_and_commits(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    home.mkdir()
    vault.mkdir()
    _write_home_config(home, vault)
    env = _git_env(home)

    _git(vault, "init", env=env)
    note = vault / "notes" / "deleted.md"
    note.parent.mkdir()
    note.write_text("---\ntags: [test]\n---\n\nRemember me.\n", encoding="utf-8")
    _git(vault, "add", "notes/deleted.md", env=env)
    _git(vault, "commit", "-m", "baseline", env=env)
    note.unlink()

    result = subprocess.run(
        [str(REPO_ROOT / "hooks" / "vault-commit.sh"), "test: delete note"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    tombstone_lines = (vault / ".memento" / "tombstones.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(tombstone_lines) == 1
    tombstone = json.loads(tombstone_lines[0])
    assert tombstone["path"] == "notes/deleted.md"
    assert tombstone["reason"] == "deleted"
    assert len(tombstone["content_hash"]) == 64
    assert _git(vault, "log", "-1", "--pretty=%s", env=env).stdout.strip() == "test: delete note"
    assert _git(vault, "status", "--short", env=env).stdout == ""


def test_wait_and_commit_normalizes_tags_removes_sentinel_and_runs_commit_script(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    home.mkdir()
    (vault / "notes").mkdir(parents=True)
    note = vault / "notes" / "taggy.md"
    note.write_text("---\ntags: [Py, k8s, Py]\n---\n\nBody.\n", encoding="utf-8")
    sentinel = tmp_path / "done.sentinel"
    sentinel.write_text("done", encoding="utf-8")
    called = tmp_path / "commit-called.txt"
    commit_script = tmp_path / "commit.sh"
    commit_script.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$1\" > {called}\n", encoding="utf-8")
    commit_script.chmod(0o755)

    env = os.environ.copy()
    env.update({"HOME": str(home), "MEMENTO_VAULT_PATH": str(vault)})
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "hooks" / "wait-and-commit.py"),
            str(sentinel),
            "0",
            str(REPO_ROOT / "hooks"),
            str(commit_script),
            "test: wait commit",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
    assert called.read_text(encoding="utf-8").strip() == "test: wait commit"
    assert "tags: [python, kubernetes]" in note.read_text(encoding="utf-8")


def test_reindex_qmd_spawns_detached_update_and_embed_when_qmd_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    qmd = fake_bin / "qmd"
    qmd.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    qmd.chmod(0o755)
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    home.mkdir()
    vault.mkdir()
    _write_home_config(home, vault, 'qmd_collection: "memento-test"\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    import memento.config as memento_config

    memento_config.reset_config()
    spec = importlib.util.spec_from_file_location("memento_triage_for_test", REPO_ROOT / "hooks" / "memento-triage.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    calls: list[dict[str, object]] = []

    class FakePopen:
        def __init__(self, args: list[str], **kwargs: object) -> None:
            calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(module.subprocess, "Popen", FakePopen)
    module.reindex_qmd(delay_seconds=7)

    assert len(calls) == 1
    args = calls[0]["args"]
    kwargs = calls[0]["kwargs"]
    assert isinstance(args, list)
    assert args[0] == sys.executable
    assert args[1] == "-c"
    assert args[3] == "7"
    assert args[4] == "memento-test"
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    memento_config.reset_config()
