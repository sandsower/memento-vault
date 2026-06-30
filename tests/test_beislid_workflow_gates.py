from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".beislid" / "workflow.md"

EXPECTED_COMMANDS = {
    "ruff-check": ".venv/bin/python -m ruff check .",
    "ruff-format-check": ".venv/bin/python -m ruff format --check .",
    "python-compileall": ".venv/bin/python -m compileall -q memento hooks scripts",
    "frontmatter-schema-drift": ".venv/bin/python scripts/check_frontmatter_schema.py",
    "targeted-tests": ".venv/bin/python -m pytest tests/test_llm_backends.py tests/test_lifecycle.py tests/test_triage.py tests/test_store.py tests/test_frontmatter_schema.py tests/test_script_harnesses.py",
    "retrieval-tests": ".venv/bin/python -m pytest tests/test_tenet_*.py tests/test_multi_hop.py tests/test_deep_recall.py",
    "mcp-server-tests": ".venv/bin/python -m pytest tests/test_mcp_server.py tests/test_remote_client.py tests/test_integration_remote.py",
    "install-tests": ".venv/bin/python -m pytest tests/test_install_helpers.py tests/test_install_register_mcp.py",
    "release-smoke": ".venv/bin/python scripts/release_smoke.py",
    "install-exec-smoke": ".venv/bin/python scripts/release_smoke.py --install-exec",
}


def _gates_block() -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"```beislid:gates\n(?P<body>.*?)\n```", workflow, re.DOTALL)
    assert match, "workflow.md must define a beislid:gates block"
    return match.group("body")


def _gate_entries() -> dict[str, str]:
    entries: dict[str, str] = {}
    for match in re.finditer(r"(?m)^- name: (?P<name>[^\n]+)\n", _gates_block()):
        start = match.start()
        next_match = re.search(r"(?m)^- name: ", _gates_block()[match.end() :])
        end = match.end() + next_match.start() if next_match else len(_gates_block())
        entries[match.group("name").strip()] = _gates_block()[start:end]
    return entries


def test_beislid_gates_preserve_existing_command_surface() -> None:
    gates = _gate_entries()

    assert set(gates) == set(EXPECTED_COMMANDS)
    for name, command in EXPECTED_COMMANDS.items():
        assert f"command: '{command}'" in gates[name]


def test_beislid_gates_are_rich_pre_pr_sensors() -> None:
    gates = _gate_entries()

    for name, body in gates.items():
        assert "stage: pre-pr" in body, name
        assert "kind: sensor" in body, name
        assert "execution: computational" in body, name
        assert "timeout_seconds:" in body, name
        assert "cost:" in body, name
        assert "mutates: false" in body, name
        assert "changed_file_selector:" in body, name
        assert "include:" in body, name
        assert "output:" in body, name
        assert "parser:" in body, name
        assert "agent_summary: true" in body, name
        assert "failure:" in body, name
        assert "retryable: true" in body, name
        assert "hint:" in body, name


def test_beislid_gate_migration_policy_is_documented() -> None:
    docs = (REPO_ROOT / "docs" / "beislid-gates.md").read_text(encoding="utf-8")
    releasing = (REPO_ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")

    assert "Flat gates are still valid Beislið input" in docs
    assert "pre-pr" in docs
    assert "Rondo consumers should treat the gate list as a metadata source" in docs
    assert "beislid-gates.md" in releasing
