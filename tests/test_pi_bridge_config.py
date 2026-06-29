from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_MODULE = (REPO_ROOT / "extensions" / "memento-config.js").as_uri()


def _run_node_load_config(cwd: Path, home: Path, env_overrides: dict[str, str] | None = None) -> dict:
    env = os.environ.copy()
    env.update(env_overrides or {})
    env["HOME"] = str(home)
    script = f"""
const mod = await import({json.dumps(CONFIG_MODULE)});
const payload = mod.loadConfig({json.dumps(str(cwd))});
console.log(JSON.stringify(payload));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_pi_bridge_defaults_enable_briefing_recall_tool_context_and_capture(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()

    payload = _run_node_load_config(project, home)
    config = payload["config"]

    assert payload["sources"] == ["defaults"]
    assert config["briefing"] is True
    assert config["promptRecall"] is True
    assert config["toolContext"] is True
    assert config["autoCapture"] is True
    assert config["captureQueue"] is True
    assert config["processQueue"] is True


def test_pi_bridge_config_layers_files_and_env_over_defaults(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    config_home = home / ".config" / "memento-vault"
    config_home.mkdir(parents=True)
    project_pi = project / ".pi"
    project_pi.mkdir(parents=True)
    project.mkdir(exist_ok=True)

    (config_home / "pi-bridge.json").write_text(
        json.dumps({"memento": {"piBridge": {"briefing": False, "toolContext": False, "autoCapture": False}}})
    )
    (project_pi / "settings.json").write_text(
        json.dumps({"piBridge": {"autoCapture": True, "processQueueOnSessionClose": True}})
    )
    (project / "package.json").write_text(json.dumps({"memento": {"piBridge": {"promptRecall": False}}}))

    payload = _run_node_load_config(project, home, {"MEMENTO_PI_TOOL_CONTEXT": "true"})
    config = payload["config"]

    assert config["briefing"] is False
    assert config["promptRecall"] is False
    assert config["toolContext"] is True
    assert config["autoCapture"] is True
    assert config["processQueueOnSessionClose"] is True
    assert payload["sources"][0] == "defaults"
    assert payload["sources"][1].endswith("pi-bridge.json")
    assert payload["sources"][2].endswith(".pi/settings.json")
    assert payload["sources"][3].endswith("package.json")
    assert payload["sources"][4] == "environment"
