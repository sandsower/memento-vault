from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".beislid" / "workflow.md"
Rondo_WORKFLOW = REPO_ROOT / "WORKFLOW.md"
ARTIFACT = REPO_ROOT / ".beislid" / "rondo-process-artifact.json"
ACTION_POLICY = REPO_ROOT / ".beislid" / "action-policy.json"

EXPECTED_COMMANDS = {
    "ruff-check": ".venv/bin/python -m ruff check .",
    "ruff-format-check": ".venv/bin/python -m ruff format --check .",
    "python-compileall": ".venv/bin/python -m compileall -q memento hooks scripts",
    "frontmatter-schema-drift": ".venv/bin/python scripts/check_frontmatter_schema.py",
    "targeted-tests": ".venv/bin/python -m pytest tests/test_llm_backends.py tests/test_lifecycle.py tests/test_triage.py tests/test_store.py tests/test_embedded_search.py tests/test_frontmatter_schema.py tests/test_script_harnesses.py tests/test_beislid_workflow_gates.py",
    "retrieval-tests": ".venv/bin/python -m pytest tests/test_tenet_*.py tests/test_multi_hop.py tests/test_deep_recall.py",
    "mcp-server-tests": ".venv/bin/python -m pytest tests/test_mcp_server.py tests/test_remote_client.py tests/test_integration_remote.py",
    "install-tests": ".venv/bin/python -m pytest tests/test_install_helpers.py tests/test_install_register_mcp.py",
    "release-smoke": ".venv/bin/python scripts/release_smoke.py",
    "install-exec-smoke": ".venv/bin/python scripts/release_smoke.py --install-exec",
    "eval-framework-tests": ".venv/bin/python -m pytest tests/test_evals.py",
    "retrieval-probe-fixture": ".venv/bin/python evals/retrieval_probe.py --mode fixture --strict",
    "capture-e2e-hermetic": ".venv/bin/python evals/run_evals.py --suite capture_e2e",
    "capture-retrieve-loop-hermetic": ".venv/bin/python evals/run_evals.py --suite capture_retrieve_loop",
}


def _gates_block() -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"```beislid:gates\n(?P<body>.*?)\n```", workflow, re.DOTALL)
    assert match, "workflow.md must define a beislid:gates block"
    return match.group("body")


def _gate_entries() -> dict[str, str]:
    entries: dict[str, str] = {}
    block = _gates_block()
    for match in re.finditer(r"(?m)^- name: (?P<name>[^\n]+)\n", block):
        start = match.start()
        next_match = re.search(r"(?m)^- name: ", block[match.end() :])
        end = match.end() + next_match.start() if next_match else len(block)
        entries[match.group("name").strip()] = block[start:end]
    return entries


def _artifact() -> dict[str, object]:
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def _action_policy() -> dict[str, object]:
    return json.loads(ACTION_POLICY.read_text(encoding="utf-8"))


def _artifact_gate_names(paths: list[str], stage: str = "post_turn") -> list[str]:
    artifact = _artifact()
    selected: list[str] = []
    seen: set[str] = set()

    for gate_set in artifact["gate_sets"]:
        assert isinstance(gate_set, dict)
        selectors = gate_set["paths"]
        assert isinstance(selectors, list)
        if not any(_selector_matches(path, selector) for path in paths for selector in selectors):
            continue

        for gate in gate_set["gates"]:
            assert isinstance(gate, dict)
            if gate.get("stage") not in (stage, None, "shared"):
                continue
            name = gate["name"]
            if name not in seen:
                selected.append(name)
                seen.add(name)

    return selected


def _selector_matches(path: str, selector: str) -> bool:
    path = path.strip().replace("\\", "/").removeprefix("./")
    selector = selector.strip().replace("\\", "/").removeprefix("./")

    if selector.endswith("/"):
        return path.startswith(selector)

    if "*" in selector or "?" in selector:
        return bool(_glob_regex(selector).match(path))

    return path == selector or path.startswith(f"{selector}/")


def _glob_regex(selector: str) -> re.Pattern[str]:
    regex = re.escape(selector)
    regex = regex.replace(r"\*\*/", r"(?:.*/)?")
    regex = regex.replace(r"\*\*", r".*")
    regex = regex.replace(r"\*", r"[^/]*")
    regex = regex.replace(r"\?", r"[^/]")
    return re.compile(f"^{regex}$")


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

        if name == "targeted-tests":
            assert ".beislid/**" in body
            assert "WORKFLOW.md" in body
            assert "docs/**" in body
            assert "tests/test_beislid_workflow_gates.py" in body

        if name == "mcp-server-tests":
            assert "memento/auth.py" in body


def test_beislid_process_artifact_and_workflow_wiring() -> None:
    artifact = _artifact()
    policy = _action_policy()
    rondo_workflow = Rondo_WORKFLOW.read_text(encoding="utf-8")
    docs = (REPO_ROOT / "docs" / "history" / "beislid-gates.md").read_text(encoding="utf-8")

    assert artifact["schema"] == "beislid-process-artifact-v1"
    assert artifact["id"] == "rondo-mem-20-process-artifact"
    assert artifact["status"] == "approved"
    assert artifact["action_policy"] == {"decision": "allow"}
    assert artifact["gate_sets"]

    gate_action_ids = {gate["action_id"] for gate_set in artifact["gate_sets"] for gate in gate_set["gates"]}
    assert gate_action_ids == {
        "gate.frontmatter-schema-drift",
        "gate.targeted-tests",
        "gate.ruff-check",
        "gate.ruff-format-check",
        "gate.python-compileall",
        "gate.retrieval-tests",
        "gate.mcp-server-tests",
        "gate.install-tests",
        "gate.release-smoke",
        "gate.install-exec-smoke",
        "gate.retrieval-probe-fixture",
        "gate.eval-framework-tests",
        "gate.capture-retrieve-loop-hermetic",
    }

    for mode in ("supervised-auto", "unattended-auto"):
        actions = policy["modes"][mode]["actions"]
        assert "gate.frontmatter-schema-drift" in actions
        assert gate_action_ids <= set(actions)

    assert (
        "process_provider:\n  kind: beislid\n  required: true\n  artifact_path: .beislid/rondo-process-artifact.json"
        in rondo_workflow
    )
    assert "tests/test_beislid_workflow_gates.py" in rondo_workflow
    assert (
        "command: .venv/bin/python -m pytest tests/test_llm_backends.py tests/test_lifecycle.py tests/test_triage.py tests/test_store.py tests/test_frontmatter_schema.py tests/test_script_harnesses.py tests/test_beislid_workflow_gates.py"
        in rondo_workflow
    )
    assert "rondo-process-artifact.json" in docs
    assert "selected gate" in docs
    assert "unmatched paths" in docs

    assert _artifact_gate_names([".beislid/workflow.md"]) == ["targeted-tests"]
    assert set(_artifact_gate_names(["docs/frontmatter-schema.md"])) == {
        "frontmatter-schema-drift",
        "targeted-tests",
    }
    assert set(_artifact_gate_names(["hooks/vault-commit.sh"])) == {"targeted-tests"}
    assert set(_artifact_gate_names(["memento/mcp_server.py"])) == {
        "ruff-check",
        "ruff-format-check",
        "python-compileall",
        "targeted-tests",
        "mcp-server-tests",
    }
    assert set(_artifact_gate_names(["memento/auth.py"])) == {
        "ruff-check",
        "ruff-format-check",
        "python-compileall",
        "targeted-tests",
        "mcp-server-tests",
    }
    assert set(_artifact_gate_names(["memento/types.py"])) == {
        "frontmatter-schema-drift",
        "targeted-tests",
        "ruff-check",
        "ruff-format-check",
        "python-compileall",
    }
    assert set(_artifact_gate_names(["memento/search.py"])) == {
        "ruff-check",
        "ruff-format-check",
        "python-compileall",
        "targeted-tests",
        "retrieval-tests",
    }
    assert set(_artifact_gate_names(["install.sh"])) == {
        "install-tests",
        "release-smoke",
        "install-exec-smoke",
    }
    assert set(_artifact_gate_names(["memento/retrieval_policy.py"])) == {
        "ruff-check",
        "ruff-format-check",
        "python-compileall",
        "targeted-tests",
        "retrieval-probe-fixture",
        "eval-framework-tests",
        "capture-retrieve-loop-hermetic",
    }
    assert set(_artifact_gate_names(["memento/lifecycle.py"])) == {
        "ruff-check",
        "ruff-format-check",
        "python-compileall",
        "targeted-tests",
        "retrieval-probe-fixture",
        "eval-framework-tests",
        "capture-retrieve-loop-hermetic",
    }
    assert set(_artifact_gate_names(["evals/retrieval_probe.py"])) == {
        "retrieval-probe-fixture",
        "eval-framework-tests",
        "capture-retrieve-loop-hermetic",
    }

    assert "Flat gates are still valid Beislið input" in docs
    assert "post-turn process-artifact selector set" in docs
    assert "changed-file-aware subset" in docs
    assert "full pre-PR gate catalog" in docs


def test_beislid_gate_migration_policy_is_documented() -> None:
    docs = (REPO_ROOT / "docs" / "history" / "beislid-gates.md").read_text(encoding="utf-8")
    releasing = (REPO_ROOT / "docs" / "history" / "releasing.md").read_text(encoding="utf-8")

    assert "Flat gates are still valid Beislið input" in docs
    assert "pre-pr" in docs
    assert "Rondo consumers should treat the gate list as a metadata source" in docs
    assert "beislid-gates.md" in releasing
