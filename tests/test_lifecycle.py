import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from memento.config import DEFAULT_CONFIG
from memento.lifecycle import (
    LifecycleResult,
    build_recall,
    build_tool_context,
    empty_result,
    is_low_signal_recall_prompt,
    should_append_project_to_recall,
)


def test_lifecycle_result_to_dict_includes_required_fields():
    result = LifecycleResult(
        should_inject=True,
        content="[vault] context",
        source="recall",
        results=[{"path": "notes/example.md", "title": "Example"}],
    )

    assert result.to_dict() == {
        "should_inject": True,
        "content": "[vault] context",
        "source": "recall",
        "results": [{"path": "notes/example.md", "title": "Example"}],
    }


def test_lifecycle_result_to_dict_includes_reason_and_metadata_when_present():
    result = LifecycleResult(
        should_inject=False,
        content="",
        source="tool-context",
        reason="skipped-path",
        metadata={"cwd": "/repo", "session_id": "s1"},
    )

    assert result.to_dict() == {
        "should_inject": False,
        "content": "",
        "source": "tool-context",
        "results": [],
        "reason": "skipped-path",
        "metadata": {"cwd": "/repo", "session_id": "s1"},
    }


def test_empty_result_defaults_to_no_results_reason():
    assert empty_result("briefing").to_dict() == {
        "should_inject": False,
        "content": "",
        "source": "briefing",
        "results": [],
        "reason": "no-results",
    }


@pytest.mark.parametrize(
    "prompt",
    [
        "go for it",
        "go for the next",
        "go for the extensions cleanup",
        "continue",
        "do it",
        "what is the next slice?",
        "ship it",
        "start fresh",
        "lets start fresh",
    ],
)
def test_low_signal_recall_prompt_gate_matches_observed_noise(prompt):
    assert is_low_signal_recall_prompt(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "how should pi lifecycle capture queue flushing work",
        "what did we decide about MCP lifecycle tools",
        "continue with the dual extract and dedup",
        "go for tolgee sync",
        "ship DC-4956 backend ticket",
    ],
)
def test_low_signal_recall_prompt_gate_allows_domain_bearing_prompts(prompt):
    assert is_low_signal_recall_prompt(prompt) is False


def test_project_slug_append_requires_signal():
    assert should_append_project_to_recall("go for the extensions cleanup") is False
    assert should_append_project_to_recall("how should pi lifecycle capture queue flushing work") is True


@patch("memento.lifecycle.get_config", return_value={"prompt_recall": True})
def test_build_recall_skips_low_signal_prompt(_config):
    result = build_recall("go for the extensions cleanup", "/home/vic/Projects/memento-vault", "s1")

    assert result.should_inject is False
    assert result.reason == "low-signal-prompt"


@patch("memento.lifecycle.log_retrieval")
@patch("memento.lifecycle.get_config", return_value={"prompt_recall": True, "recall_diagnostics": False})
def test_recall_diagnostics_disabled_by_default(_config, mock_log):
    build_recall("go for the extensions cleanup", "/home/vic/Projects/memento-vault", "s1")

    actions = [call.args[1] for call in mock_log.call_args_list]
    assert "low-signal-prompt" in actions
    assert not any(action.startswith("diagnostic-") for action in actions)


@patch("memento.lifecycle.log_retrieval")
@patch(
    "memento.lifecycle.get_config",
    return_value={"prompt_recall": True, "recall_diagnostics": True, "recall_diagnostics_include_candidates": False},
)
def test_recall_diagnostics_logs_skip_decision(_config, mock_log):
    build_recall("go for the extensions cleanup", "/home/vic/Projects/memento-vault", "s1")

    actions = [call.args[1] for call in mock_log.call_args_list]
    assert "diagnostic-start" in actions
    assert "diagnostic-skip" in actions
    assert "diagnostic-decision" in actions
    decision = [call.kwargs for call in mock_log.call_args_list if call.args[1] == "diagnostic-decision"][-1]
    assert decision == {"decision": "skipped", "reason": "low-signal-prompt"}


@patch("memento.lifecycle.log_retrieval")
@patch("memento.lifecycle.enhance_results", side_effect=lambda results, *args, **kwargs: results)
@patch("memento.lifecycle.qmd_search_with_extras")
@patch("memento.lifecycle.has_qmd", return_value=True)
@patch("memento.lifecycle.get_vault")
@patch(
    "memento.lifecycle.get_config",
    return_value={
        "prompt_recall": True,
        "recall_diagnostics": True,
        "recall_diagnostics_include_candidates": True,
        "recall_diagnostics_max_candidates": 1,
        "recall_min_score": 0.4,
        "recall_max_notes": 3,
        "recall_high_confidence": 0.55,
        "concept_index_enabled": False,
        "rrf_enabled": False,
        "multi_hop_enabled": False,
        "reranker_enabled": False,
    },
)
def test_recall_diagnostics_candidate_logging_is_capped(
    _config, mock_vault, _has_qmd, mock_search, _enhance, mock_log, tmp_path
):
    (tmp_path / "notes").mkdir()
    mock_vault.return_value = tmp_path
    mock_search.return_value = [
        {"path": "notes/a.md", "title": "A", "score": 0.9, "snippet": "A"},
        {"path": "notes/b.md", "title": "B", "score": 0.8, "snippet": "B"},
    ]

    result = build_recall("how should pi lifecycle capture queue flushing work", "/repo", "s1")

    assert result.should_inject is True
    candidate_events = [call.kwargs for call in mock_log.call_args_list if call.args[1] == "diagnostic-candidates"]
    assert candidate_events
    assert len(candidate_events[0]["candidates"]) == 1
    assert candidate_events[0]["candidates"][0] == {
        "path": "notes/a.md",
        "title": "A",
        "score": 0.9,
        "decision": "candidate",
    }


def test_tool_context_skips_unsupported_tool():
    result = build_tool_context("bash", "src/server/authMiddleware.ts", "/repo", "s1")

    assert result.to_dict()["reason"] == "unsupported-tool"


def test_tool_context_skips_missing_file_path():
    result = build_tool_context("Read", "", "/repo", "s1")

    assert result.to_dict()["reason"] == "missing-file-path"


def test_tool_context_skips_system_and_config_paths():
    assert build_tool_context("Read", "/usr/lib/python.py", "/repo", "s1").reason == "skipped-path"
    assert build_tool_context("Read", "package.json", "/repo", "s1").reason == "skipped-path"


def test_tool_context_skips_agent_skill_and_memory_files():
    assert (
        build_tool_context("Read", "/home/vic/.claude/skills/continue-work/SKILL.md", "/repo", "s1").reason
        == "skipped-path"
    )
    assert build_tool_context("Read", "/home/vic/.agents/skills/debug/SKILL.md", "/repo", "s1").reason == "skipped-path"
    assert build_tool_context("Read", "/home/vic/.codex/memories/MEMORY.md", "/repo", "s1").reason == "skipped-path"
    assert build_tool_context("Read", "/repo/.pi/settings.json", "/repo", "s1").reason == "skipped-path"


def test_tool_context_skips_memento_bridge_adapter_files():
    assert build_tool_context("Read", "/repo/extensions/memento.ts", "/repo", "s1").reason == "skipped-path"
    assert build_tool_context("Read", "/repo/memento/pi_bridge.py", "/repo", "s1").reason == "skipped-path"


@patch("memento.lifecycle.has_qmd", return_value=True)
def test_tool_context_skips_insufficient_keywords(_has_qmd):
    with patch("memento.lifecycle.load_cache", return_value={"dirs": {}, "last_qmd_call": 0, "injections": {}}):
        with patch("memento.lifecycle.save_cache"):
            result = build_tool_context("Read", "/workspace/src/a.py", "/repo", "s1")

    assert result.reason == "insufficient-keywords"


@patch("memento.lifecycle.log_retrieval")
@patch("memento.lifecycle.enhance_results", side_effect=lambda results, *args, **kwargs: results)
@patch("memento.lifecycle.qmd_search_with_extras")
@patch("memento.lifecycle.has_qmd", return_value=True)
def test_tool_context_searches_and_formats_results(_has_qmd, mock_search, _enhance, _log):
    mock_search.return_value = [
        {
            "path": "notes/auth-boundary.md",
            "title": "Auth boundary lives in middleware",
            "score": 0.78,
            "snippet": "Middleware owns auth checks.",
        }
    ]

    config = dict(DEFAULT_CONFIG)
    config["tool_context_min_score"] = 0.75
    with patch("memento.lifecycle.get_config", return_value=config):
        with patch("memento.lifecycle.load_cache", return_value={"dirs": {}, "last_qmd_call": 0, "injections": {}}):
            with patch("memento.lifecycle.save_cache"):
                result = build_tool_context("Read", "src/server/authMiddleware.ts", "/repo", "s1")

    assert result.should_inject is True
    assert result.source == "tool-context"
    assert result.content.startswith("[connected-to-vault]")
    assert "Auth boundary lives in middleware" in result.content
    mock_search.assert_called_once()
    _, kwargs = mock_search.call_args
    assert kwargs["semantic"] is False
    assert kwargs["min_score"] == 0.75


def test_tool_context_hook_adapter_outputs_claude_json(capsys):
    hook_path = Path(__file__).parent.parent / "hooks" / "vault-tool-context.py"
    spec = importlib.util.spec_from_file_location("vault_tool_context_hook", hook_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    hook_input = {
        "tool_name": "Read",
        "tool_input": {"file_path": "src/server/authMiddleware.ts"},
        "cwd": "/repo",
        "session_id": "s1",
    }
    result = LifecycleResult(True, "[connected-to-vault]\n  - Auth boundary", "tool-context")
    with patch.object(module, "read_hook_input", return_value=hook_input):
        with patch.object(module, "build_tool_context", return_value=result) as mock_build:
            module.main()

    mock_build.assert_called_once_with("Read", "src/server/authMiddleware.ts", "/repo", "s1")
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": "[connected-to-vault]\n  - Auth boundary",
        }
    }
