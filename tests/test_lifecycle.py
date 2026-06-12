import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from memento.config import DEFAULT_CONFIG
from memento.lifecycle import (
    LifecycleResult,
    _run_recall_lines,
    build_recall,
    build_session_context,
    build_tool_context,
    empty_result,
    filter_recall_results_by_explicit_project,
    is_broad_project_history_query,
    is_low_signal_recall_prompt,
    should_append_project_to_recall,
    triage_health_warning,
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


def test_build_session_context_combines_briefing_recall_status_and_queue(tmp_path):
    queue_file = tmp_path / "queue" / "pi-captures.jsonl"
    queue_file.parent.mkdir()
    queue_file.write_text('{"id":"q1"}\n')
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("# A")

    briefing = LifecycleResult(True, "[vault] Project: repo | 1 sessions | 1 notes", "briefing")
    recall = LifecycleResult(
        True,
        "[vault] Related memories:\n  - Cache policy: Use TTLs.",
        "recall",
        results=[{"path": "notes/cache.md", "title": "Cache policy", "snippet": "Use TTLs."}],
        metadata={"top_path": "notes/cache.md"},
    )

    with (
        patch("memento.lifecycle.build_briefing", return_value=briefing) as mock_briefing,
        patch("memento.lifecycle.build_recall", return_value=recall) as mock_recall,
        patch("memento.lifecycle.get_vault", return_value=tmp_path),
        patch("memento.lifecycle.has_qmd", return_value=True),
        patch("memento.lifecycle.triage_health_warning", return_value="[vault] WARN: triage failing"),
    ):
        payload = build_session_context(
            cwd="/repo",
            prompt="how should cache work?",
            session_id="s1",
            token_budget=200,
        )

    assert payload["should_inject"] is True
    assert payload["source"] == "session-context"
    assert "[vault] Project: repo" in payload["content"]
    assert "Cache policy" in payload["content"]
    assert payload["sections"]["status"]["vault_exists"] is True
    assert payload["sections"]["status"]["qmd_available"] is True
    assert payload["sections"]["queue"]["queued_capture_count"] == 1
    assert payload["metadata"]["warnings"] == ["[vault] WARN: triage failing"]
    assert payload["metadata"]["expandable_paths"] == ["notes/cache.md"]
    assert payload["metadata"]["truncated"] is False
    mock_briefing.assert_called_once_with("/repo", "s1", allow_deferred=False)
    mock_recall.assert_called_once_with("how should cache work?", "/repo", "s1", record=False)


def test_build_session_context_respects_budget_and_reports_expandable_paths(tmp_path):
    (tmp_path / "notes").mkdir()
    long_content = "[vault] Related memories:\n  - " + ("long memory " * 100)
    recall = LifecycleResult(
        True,
        long_content,
        "recall",
        results=[
            {"path": "notes/long.md", "title": "Long memory", "snippet": "long memory"},
            {"path": "notes/second.md", "title": "Second", "snippet": "second"},
        ],
    )

    with (
        patch("memento.lifecycle.build_briefing", return_value=empty_result("briefing", "disabled")),
        patch("memento.lifecycle.build_recall", return_value=recall),
        patch("memento.lifecycle.get_vault", return_value=tmp_path),
        patch("memento.lifecycle.has_qmd", return_value=True),
        patch("memento.lifecycle.triage_health_warning", return_value=None),
    ):
        payload = build_session_context("/repo", "memory", "s1", token_budget=30)

    assert len(payload["content"]) <= payload["metadata"]["char_budget"]
    assert payload["metadata"]["truncated"] is True
    assert payload["metadata"]["expandable_paths"] == ["notes/long.md", "notes/second.md"]
    assert "truncated" in payload["metadata"]["budget_notes"][0]


def test_build_session_context_compacts_structured_payload_under_budget_overhead(tmp_path):
    (tmp_path / "notes").mkdir()
    long_text = "x" * 2000
    recall = LifecycleResult(
        True,
        "[vault] Related memories:\n  - " + long_text,
        "recall",
        results=[{"path": "notes/long.md", "title": "Long memory", "snippet": long_text, "content": long_text}],
        metadata={"diagnostic": long_text},
    )

    with (
        patch("memento.lifecycle.build_briefing", return_value=empty_result("briefing", "disabled")),
        patch("memento.lifecycle.build_recall", return_value=recall),
        patch("memento.lifecycle.get_vault", return_value=tmp_path),
        patch("memento.lifecycle.has_qmd", return_value=True),
        patch("memento.lifecycle.triage_health_warning", return_value=None),
    ):
        payload = build_session_context("/repo", "memory", "s1", token_budget=30)

    serialized = json.dumps(payload)
    assert len(serialized) <= payload["metadata"]["packet_char_budget"]
    assert "content" not in payload["sections"]["recall"]
    assert "content" not in payload["results"][0]
    assert payload["metadata"]["truncated"] is True
    assert payload["metadata"]["expandable_paths"] == ["notes/long.md"]


def test_build_session_context_final_budget_fallback_handles_long_metadata(tmp_path):
    (tmp_path / "notes").mkdir()
    long_value = "x" * 5000

    with (
        patch("memento.lifecycle.build_briefing", return_value=empty_result("briefing", "disabled")),
        patch("memento.lifecycle.get_vault", return_value=tmp_path),
        patch("memento.lifecycle.has_qmd", return_value=True),
        patch("memento.lifecycle.triage_health_warning", return_value="[vault] WARN: " + long_value),
    ):
        payload = build_session_context(long_value, "", long_value, token_budget=100, include_recall=False)

    assert len(json.dumps(payload)) <= payload["metadata"]["packet_char_budget"]
    assert payload["should_inject"] == bool(payload["content"])
    assert payload["metadata"]["truncated"] is True
    assert payload["metadata"].get("omitted_metadata") is True


def test_build_session_context_recomputes_should_inject_on_early_fit(tmp_path):
    (tmp_path / "notes").mkdir()
    briefing = LifecycleResult(True, "short context", "briefing")

    with (
        patch("memento.lifecycle.build_briefing", return_value=briefing),
        patch("memento.lifecycle.get_vault", return_value=tmp_path),
        patch("memento.lifecycle.has_qmd", return_value=True),
        patch("memento.lifecycle.triage_health_warning", return_value="[vault] WARN: " + ("x" * 1000)),
    ):
        payload = build_session_context("/repo", "", "s1", token_budget=50, include_recall=False)

    assert len(json.dumps(payload)) <= payload["metadata"]["packet_char_budget"]
    assert payload["content"] == ""
    assert payload["should_inject"] is False
    assert payload["metadata"]["used_chars"] == 0


def test_build_session_context_records_recall_only_after_final_payload_includes_it(tmp_path):
    (tmp_path / "notes").mkdir()

    with (
        patch("memento.lifecycle.build_briefing", return_value=empty_result("briefing", "disabled")),
        patch(
            "memento.lifecycle._run_recall_lines",
            return_value=(
                ["[vault] Related memories:", "  - Cache policy: Use TTLs."],
                "notes/cache.md",
                [{"path": "notes/cache.md", "title": "Cache policy"}],
                None,
            ),
        ),
        patch("memento.lifecycle.record_recall") as mock_record,
        patch("memento.lifecycle.get_vault", return_value=tmp_path),
        patch("memento.lifecycle.has_qmd", return_value=True),
        patch("memento.lifecycle.triage_health_warning", return_value=None),
    ):
        payload = build_session_context("/repo", "cache", "s1", token_budget=2000)

    assert "Cache policy" in payload["content"]
    mock_record.assert_called_once_with("notes/cache.md")


def test_build_session_context_does_not_record_recall_when_final_payload_drops_content(tmp_path):
    (tmp_path / "notes").mkdir()

    with (
        patch("memento.lifecycle.build_briefing", return_value=empty_result("briefing", "disabled")),
        patch(
            "memento.lifecycle._run_recall_lines",
            return_value=(
                ["[vault] Related memories:", "  - Cache policy: Use TTLs."],
                "notes/cache.md",
                [{"path": "notes/cache.md", "title": "Cache policy", "snippet": "x" * 2000}],
                None,
            ),
        ),
        patch("memento.lifecycle.record_recall") as mock_record,
        patch("memento.lifecycle.get_vault", return_value=tmp_path),
        patch("memento.lifecycle.has_qmd", return_value=True),
        patch("memento.lifecycle.triage_health_warning", return_value="[vault] WARN: " + ("x" * 1000)),
    ):
        payload = build_session_context("/repo", "cache", "s1", token_budget=1)

    assert payload["content"] == ""
    mock_record.assert_not_called()


def test_build_session_context_disables_deferred_briefing_work(tmp_path):
    (tmp_path / "notes").mkdir()

    with (
        patch("memento.lifecycle.get_config", return_value={"session_briefing": True, "project_maps_enabled": True}),
        patch("memento.lifecycle.get_vault", return_value=tmp_path),
        patch("memento.lifecycle.get_git_branch", return_value="feature"),
        patch("memento.lifecycle.detect_project", return_value=("repo", None)),
        patch("memento.lifecycle.read_project_index", return_value=([], [])),
        patch("memento.lifecycle.triage_health_warning", return_value=None),
        patch("memento.lifecycle.has_qmd", return_value=True),
        patch("memento.lifecycle.load_or_build_graph"),
        patch("memento.lifecycle.spawn_deferred_search") as mock_spawn,
    ):
        payload = build_session_context("/repo", "", "s1", include_recall=False)

    assert payload["sections"]["briefing"]["should_inject"] is True
    mock_spawn.assert_not_called()


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


@pytest.mark.parametrize(
    "prompt",
    [
        "what previous decisions did we make on Fundid?",
        "what do we know about Fundid?",
        "summarize Fundid history",
        "what was decided before about Fundid?",
    ],
)
def test_broad_project_history_query_gate_matches_spec_examples(prompt):
    assert is_broad_project_history_query(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "what did we decide about Fundid server-side email dispatch?",
        "how should pi lifecycle capture queue flushing work?",
        "what did we decide about MCP lifecycle tools?",
    ],
)
def test_broad_project_history_query_gate_allows_specific_prompts(prompt):
    assert is_broad_project_history_query(prompt) is False


@patch("memento.lifecycle.get_config", return_value={"prompt_recall": True})
def test_build_recall_skips_low_signal_prompt(_config):
    result = build_recall("go for the extensions cleanup", "/home/vic/Projects/memento-vault", "s1")

    assert result.should_inject is False
    assert result.reason == "low-signal-prompt"


@patch("memento.lifecycle.get_config", return_value={"prompt_recall": True})
def test_build_recall_skips_broad_project_history_prompt(_config):
    result = build_recall("what previous decisions did we make on Fundid?", "/home/vic/Projects/memento-vault", "s1")

    assert result.should_inject is False
    assert result.reason == "broad-project-query"


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
@patch(
    "memento.lifecycle.get_config",
    return_value={"prompt_recall": True, "recall_diagnostics": True, "recall_diagnostics_include_candidates": False},
)
def test_recall_diagnostics_logs_broad_project_skip_detail(_config, mock_log):
    build_recall("what do we know about Fundid?", "/home/vic/Projects/memento-vault", "s1")

    skip = [call.kwargs for call in mock_log.call_args_list if call.args[1] == "diagnostic-skip"][-1]
    assert skip["reason"] == "broad-project-query"
    assert skip["broad_project_query"] is True


@patch("memento.remote_client.is_remote", return_value=False)
@patch("memento.lifecycle.qmd_search_with_extras")
@patch("memento.lifecycle.has_qmd", return_value=True)
@patch("memento.lifecycle.get_vault")
@patch("memento.lifecycle.get_config", return_value={"prompt_recall": True, "recall_diagnostics": True})
def test_run_recall_lines_broad_project_skip_does_not_search(
    _config, mock_vault, _has_qmd, mock_search, _is_remote, tmp_path
):
    (tmp_path / "notes").mkdir()
    mock_vault.return_value = tmp_path

    lines, top_path, results, reason = _run_recall_lines("what do we know about Fundid?", str(tmp_path), "s1")

    assert (lines, top_path, results, reason) == ([], None, [], "broad-project-query")
    mock_search.assert_not_called()


@patch("memento.remote_client.is_remote", return_value=False)
@patch("memento.lifecycle.is_duplicate", return_value=False)
@patch("memento.lifecycle.enhance_results", side_effect=lambda results, *args, **kwargs: results)
@patch("memento.lifecycle.qmd_search_with_extras")
@patch("memento.lifecycle.has_qmd", return_value=True)
@patch("memento.lifecycle.get_vault")
@patch(
    "memento.lifecycle.get_config",
    return_value={
        "prompt_recall": True,
        "recall_diagnostics": True,
        "recall_diagnostics_include_candidates": False,
        "recall_min_score": 0.4,
        "recall_max_notes": 3,
        "recall_high_confidence": 0.55,
        "concept_index_enabled": False,
        "rrf_enabled": False,
        "multi_hop_enabled": False,
        "reranker_enabled": False,
    },
)
def test_run_recall_lines_specific_project_prompt_searches(
    _config, mock_vault, _has_qmd, mock_search, _enhance, _is_duplicate, _is_remote, tmp_path
):
    (tmp_path / "notes").mkdir()
    mock_vault.return_value = tmp_path
    mock_search.return_value = [{"path": "notes/fundid.md", "title": "Fundid email", "score": 0.9, "project": "fundid"}]

    lines, top_path, results, reason = _run_recall_lines(
        "what did we decide about Fundid server-side email dispatch?", str(tmp_path), "s1"
    )

    assert reason is None
    assert top_path == "notes/fundid.md"
    assert results == [{"path": "notes/fundid.md", "title": "Fundid email", "score": 0.9, "project": "fundid"}]
    assert lines == ["[vault] Related memories:", "  - Fundid email"]
    mock_search.assert_called_once()


@patch("memento.remote_client.is_remote", return_value=True)
@patch("memento.remote_client.search")
@patch("memento.lifecycle.qmd_search_with_extras")
@patch("memento.lifecycle.get_config", return_value={"prompt_recall": True, "recall_diagnostics": True})
def test_run_recall_lines_remote_broad_project_skip_does_not_search(
    _config, mock_local_search, mock_remote_search, _is_remote
):
    lines, top_path, results, reason = _run_recall_lines("what do we know about Fundid?", "/repo", "s1")

    assert (lines, top_path, results, reason) == ([], None, [], "broad-project-query")
    mock_remote_search.assert_not_called()
    mock_local_search.assert_not_called()


@patch("memento.remote_client.is_remote", return_value=True)
@patch("memento.lifecycle.is_duplicate", return_value=False)
@patch("memento.remote_client.search_envelope")
@patch("memento.lifecycle.has_qmd")
@patch(
    "memento.lifecycle.get_config",
    return_value={
        "prompt_recall": True,
        "recall_diagnostics": True,
        "recall_diagnostics_include_candidates": False,
        "recall_min_score": 0.4,
        "recall_max_notes": 3,
    },
)
def test_run_recall_lines_remote_specific_project_prompt_injects_match(
    _config, mock_has_qmd, mock_remote_search, _is_duplicate, _is_remote
):
    mock_remote_search.return_value = {
        "results": [{"path": "notes/fundid.md", "title": "Fundid email", "score": 0.9, "project": "fundid"}]
    }

    lines, top_path, results, reason = _run_recall_lines(
        "what did we decide about Fundid server-side email dispatch?", "/repo", "s1"
    )

    assert reason is None
    assert top_path == "notes/fundid.md"
    assert results == [{"path": "notes/fundid.md", "title": "Fundid email", "score": 0.9, "project": "fundid"}]
    assert lines == ["[vault] Related memories:", "  - Fundid email"]
    mock_remote_search.assert_called_once()
    assert mock_remote_search.call_args.kwargs["concrete"] is False
    mock_has_qmd.assert_not_called()


@patch("memento.remote_client.is_remote", return_value=True)
@patch("memento.lifecycle.log_retrieval")
@patch("memento.remote_client.search_envelope")
@patch("memento.lifecycle.has_qmd")
@patch(
    "memento.lifecycle.get_config",
    return_value={
        "prompt_recall": True,
        "recall_diagnostics": True,
        "recall_diagnostics_include_candidates": False,
        "recall_min_score": 0.4,
        "recall_max_notes": 3,
    },
)
def test_run_recall_lines_remote_project_mismatch_skips_without_candidate_diagnostics(
    _config, mock_has_qmd, mock_remote_search, mock_log, _is_remote
):
    mock_remote_search.return_value = {
        "results": [{"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.9, "project": "dala-care"}]
    }

    lines, top_path, results, reason = _run_recall_lines(
        "what did we decide about fundid server-side email dispatch?", "/repo", "s1"
    )

    assert (lines, top_path, results, reason) == ([], None, [], "project-mismatch-filtered-empty")
    mock_has_qmd.assert_not_called()
    assert not [
        call
        for call in mock_log.call_args_list
        if call.args[1] == "diagnostic-candidates" and call.kwargs.get("stage") == "remote-project-filter"
    ]


@patch("memento.remote_client.is_remote", return_value=True)
@patch("memento.lifecycle.log_retrieval")
@patch("memento.remote_client.search_envelope")
@patch(
    "memento.lifecycle.get_config",
    return_value={
        "prompt_recall": True,
        "recall_diagnostics": True,
        "recall_diagnostics_include_candidates": True,
        "recall_diagnostics_max_candidates": 10,
        "recall_min_score": 0.4,
        "recall_max_notes": 3,
    },
)
def test_run_recall_lines_remote_project_filter_logs_candidate_diagnostics_when_enabled(
    _config, mock_remote_search, mock_log, _is_remote
):
    mock_remote_search.return_value = {
        "results": [{"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.9, "project": "dala-care"}]
    }

    _run_recall_lines("what did we decide about Fundid server-side email dispatch?", "/repo", "s1")

    project_filter_events = [
        call.kwargs
        for call in mock_log.call_args_list
        if call.args[1] == "diagnostic-candidates" and call.kwargs.get("stage") == "remote-project-filter"
    ]
    assert project_filter_events == [
        {
            "stage": "remote-project-filter",
            "candidates": [
                {"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.9, "decision": "project-mismatch"}
            ],
            "query": "what did we decide about Fundid server-side email dispatch?",
        }
    ]


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


def test_explicit_project_filter_removes_project_mismatches():
    results = [
        {"path": "notes/fundid.md", "title": "Fundid email", "score": 0.9, "project": "fundid"},
        {"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.8, "project": "dala-care"},
        {"path": "notes/general.md", "title": "General email", "score": 0.7},
    ]

    filtered, decisions = filter_recall_results_by_explicit_project(
        "what did we decide about Fundid server-side email dispatch?", results
    )

    assert [result["path"] for result in filtered] == ["notes/fundid.md", "notes/general.md"]
    assert decisions == [
        {"path": "notes/fundid.md", "title": "Fundid email", "score": 0.9, "decision": "project-match"},
        {"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.8, "decision": "project-mismatch"},
        {"path": "notes/general.md", "title": "General email", "score": 0.7, "decision": "no-project-metadata"},
    ]


def test_explicit_project_filter_noops_without_explicit_project():
    results = [{"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.8, "project": "dala-care"}]

    filtered, decisions = filter_recall_results_by_explicit_project("how should lifecycle capture work?", results)

    assert filtered == results
    assert decisions == []


def test_explicit_project_filter_does_not_treat_acronyms_as_projects():
    results = [{"path": "notes/mcp.md", "title": "MCP lifecycle", "score": 0.8, "project": "memento-vault"}]

    filtered, decisions = filter_recall_results_by_explicit_project(
        "what did we decide about MCP lifecycle tools?", results
    )

    assert filtered == results
    assert decisions == []


def test_explicit_project_filter_detects_lowercase_project_subject():
    results = [{"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.8, "project": "dala-care"}]

    filtered, decisions = filter_recall_results_by_explicit_project(
        "what did we decide about fundid server-side email dispatch?", results
    )

    assert filtered == []
    assert decisions == [
        {"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.8, "decision": "project-mismatch"}
    ]


@patch("memento.lifecycle.read_note_metadata", return_value={"project": "dala-care"})
def test_explicit_project_filter_reads_local_note_metadata_when_result_metadata_was_stripped(_read_meta):
    results = [{"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.8}]

    filtered, decisions = filter_recall_results_by_explicit_project(
        "what did we decide about Fundid server-side email dispatch?", results
    )

    assert filtered == []
    assert decisions == [
        {"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.8, "decision": "project-mismatch"}
    ]


@patch("memento.remote_client.is_remote", return_value=False)
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
        "recall_diagnostics_include_candidates": False,
        "recall_min_score": 0.4,
        "recall_max_notes": 3,
        "recall_high_confidence": 0.55,
        "concept_index_enabled": False,
        "rrf_enabled": False,
        "multi_hop_enabled": False,
        "reranker_enabled": False,
    },
)
def test_run_recall_lines_project_mismatch_can_filter_everything_without_candidate_diagnostics(
    _config, mock_vault, _has_qmd, mock_search, _enhance, mock_log, _is_remote, tmp_path
):
    (tmp_path / "notes").mkdir()
    mock_vault.return_value = tmp_path
    mock_search.return_value = [
        {"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.9, "project": "dala-care"}
    ]

    lines, top_path, results, reason = _run_recall_lines(
        "what did we decide about fundid server-side email dispatch?", str(tmp_path), "s1"
    )

    assert (lines, top_path, results, reason) == ([], None, [], "project-mismatch-filtered-empty")
    assert not [
        call
        for call in mock_log.call_args_list
        if call.args[1] == "diagnostic-candidates" and call.kwargs.get("stage") == "project-filter"
    ]


@patch("memento.remote_client.is_remote", return_value=False)
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
        "recall_diagnostics_max_candidates": 10,
        "recall_min_score": 0.4,
        "recall_max_notes": 3,
        "recall_high_confidence": 0.55,
        "concept_index_enabled": False,
        "rrf_enabled": False,
        "multi_hop_enabled": False,
        "reranker_enabled": False,
    },
)
def test_run_recall_lines_project_filter_logs_candidate_diagnostics_when_enabled(
    _config, mock_vault, _has_qmd, mock_search, _enhance, mock_log, _is_remote, tmp_path
):
    (tmp_path / "notes").mkdir()
    mock_vault.return_value = tmp_path
    mock_search.return_value = [
        {"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.9, "project": "dala-care"}
    ]

    _run_recall_lines("what did we decide about Fundid server-side email dispatch?", str(tmp_path), "s1")

    project_filter_events = [
        call.kwargs
        for call in mock_log.call_args_list
        if call.args[1] == "diagnostic-candidates" and call.kwargs.get("stage") == "project-filter"
    ]
    assert len(project_filter_events) == 1
    assert project_filter_events[0]["stage"] == "project-filter"
    assert project_filter_events[0]["candidates"] == [
        {"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.9, "decision": "project-mismatch"}
    ]
    assert project_filter_events[0]["query"].startswith("what did we decide about Fundid server-side email dispatch?")


@patch("memento.remote_client.is_remote", return_value=False)
@patch("memento.lifecycle.has_qmd", return_value=False)
@patch("memento.lifecycle.get_vault")
@patch("memento.lifecycle.get_config", return_value={"prompt_recall": True, "recall_diagnostics": True})
def test_build_recall_backend_unavailable_includes_miss_metadata(_config, mock_vault, _has_qmd, _is_remote, tmp_path):
    (tmp_path / "notes").mkdir()
    mock_vault.return_value = tmp_path

    result = build_recall("how should cache invalidation work?", str(tmp_path), "s1")

    assert result.should_inject is False
    assert result.reason == "backend_unavailable"
    assert result.metadata["miss"]["reason"] == "backend_unavailable"
    assert any("memento_status" in hint for hint in result.metadata["miss"]["recovery_hints"])


@patch("memento.remote_client.is_remote", return_value=False)
@patch("memento.lifecycle.log_retrieval")
@patch("memento.lifecycle.qmd_search_with_extras")
@patch("memento.lifecycle.has_qmd", return_value=True)
@patch("memento.lifecycle.get_vault")
@patch(
    "memento.lifecycle.get_config",
    return_value={
        "prompt_recall": True,
        "recall_diagnostics": True,
        "recall_min_score": 0.9,
        "recall_max_notes": 3,
        "recall_high_confidence": 0.55,
        "concept_index_enabled": False,
        "rrf_enabled": False,
        "multi_hop_enabled": False,
        "reranker_enabled": False,
    },
)
def test_build_recall_threshold_miss_includes_miss_metadata(
    _config, mock_vault, _has_qmd, mock_search, _log, _is_remote, tmp_path
):
    (tmp_path / "notes").mkdir()
    mock_vault.return_value = tmp_path
    mock_search.side_effect = [[], [{"path": "notes/cache.md", "title": "Cache", "score": 0.2, "snippet": ""}]]

    result = build_recall("how should cache invalidation work?", str(tmp_path), "s1")

    assert result.should_inject is False
    assert result.reason == "threshold_too_high"
    assert result.metadata["miss"] == {
        "reason": "threshold_too_high",
        "recovery_hints": ["Lower min_score."],
        "details": {"min_score": 0.9},
    }


@patch("memento.remote_client.is_remote", return_value=False)
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
        "recall_diagnostics_include_candidates": False,
        "recall_min_score": 0.4,
        "recall_max_notes": 3,
        "recall_high_confidence": 0.55,
        "concept_index_enabled": False,
        "rrf_enabled": False,
        "multi_hop_enabled": False,
        "reranker_enabled": False,
    },
)
def test_build_recall_project_filter_empty_includes_miss_metadata(
    _config, mock_vault, _has_qmd, mock_search, _enhance, _log, _is_remote, tmp_path
):
    (tmp_path / "notes").mkdir()
    mock_vault.return_value = tmp_path
    mock_search.return_value = [
        {"path": "notes/dala.md", "title": "Dala scheduling", "score": 0.9, "project": "dala-care"}
    ]

    result = build_recall("what did we decide about Fundid server-side email dispatch?", str(tmp_path), "s1")

    assert result.should_inject is False
    assert result.reason == "project_filter_removed_all"
    assert result.metadata["miss"]["reason"] == "project_filter_removed_all"
    assert "cwd" not in result.metadata["miss"].get("details", {})


@patch("memento.remote_client.is_remote", return_value=True)
@patch("memento.lifecycle.log_retrieval")
@patch("memento.remote_client.search_envelope")
@patch("memento.lifecycle.has_qmd", return_value=False)
@patch(
    "memento.lifecycle.get_config",
    return_value={
        "prompt_recall": True,
        "recall_diagnostics": True,
        "recall_diagnostics_include_candidates": False,
        "recall_min_score": 0.9,
        "recall_max_notes": 3,
    },
)
def test_build_recall_preserves_remote_structured_miss_when_local_unavailable(
    _config, _has_qmd, mock_remote_search, mock_log, _is_remote, tmp_path
):
    (tmp_path / "notes").mkdir()
    mock_remote_search.return_value = {
        "results": [],
        "miss": {"reason": "threshold_too_high", "recovery_hints": ["Lower min_score."]},
    }
    with patch("memento.lifecycle.get_vault", return_value=tmp_path):
        result = build_recall("how should cache invalidation work?", str(tmp_path), "s1")

    assert result.should_inject is False
    assert result.reason == "threshold_too_high"
    assert result.metadata["miss"] == {
        "reason": "threshold_too_high",
        "recovery_hints": ["Lower min_score."],
    }
    decisions = [call.kwargs for call in mock_log.call_args_list if call.args[1] == "diagnostic-decision"]
    assert decisions[-1]["reason"] == "threshold_too_high"


@patch("memento.remote_client.is_remote", return_value=True)
@patch("memento.lifecycle.is_duplicate", return_value=False)
@patch("memento.lifecycle.enhance_results", side_effect=lambda results, *args, **kwargs: results)
@patch("memento.lifecycle.qmd_search_with_extras")
@patch("memento.lifecycle.has_qmd", return_value=True)
@patch("memento.lifecycle.get_vault")
@patch("memento.remote_client.search_envelope")
@patch(
    "memento.lifecycle.get_config",
    return_value={
        "prompt_recall": True,
        "recall_diagnostics": True,
        "recall_min_score": 0.4,
        "recall_max_notes": 3,
        "recall_high_confidence": 0.55,
        "concept_index_enabled": False,
        "rrf_enabled": False,
        "multi_hop_enabled": False,
        "reranker_enabled": False,
    },
)
def test_remote_no_exact_match_falls_back_to_local_recall(
    _config, mock_remote_search, mock_vault, _has_qmd, mock_search, _enhance, _is_duplicate, _is_remote, tmp_path
):
    (tmp_path / "notes").mkdir()
    mock_vault.return_value = tmp_path
    mock_remote_search.return_value = {
        "results": [],
        "miss": {"reason": "no_exact_match", "recovery_hints": ["Try a broader query."]},
    }
    mock_search.return_value = [{"path": "notes/cache.md", "title": "Cache policy", "score": 0.9}]

    lines, top_path, results, reason = _run_recall_lines("how should cache invalidation work?", str(tmp_path), "s1")

    assert reason is None
    assert top_path == "notes/cache.md"
    assert results == [{"path": "notes/cache.md", "title": "Cache policy", "score": 0.9}]
    assert lines == ["[vault] Related memories:", "  - Cache policy"]


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


@patch("memento.lifecycle.has_qmd", return_value=False)
def test_tool_context_resolves_relative_path_against_session_cwd(_has_qmd, tmp_path, monkeypatch):
    # The Pi bridge runs with cwd=<memento-vault checkout> while the session
    # works in another project and passes the Read tool's raw relative path.
    # The path must resolve under the session cwd, not the process cwd.
    foreign_checkout = tmp_path / "memento-vault-checkout"
    foreign_checkout.mkdir()
    session_project = tmp_path / "user-project"
    (session_project / "src").mkdir(parents=True)
    monkeypatch.chdir(foreign_checkout)

    result = build_tool_context("Read", "src/authMiddleware.ts", str(session_project), "s1")

    expected = os.path.realpath(str(session_project / "src" / "authMiddleware.ts"))
    assert result.metadata["file_path"] == expected
    assert result.reason == "qmd-unavailable"


@patch("memento.lifecycle.has_qmd", return_value=False)
def test_tool_context_absolute_path_ignores_session_cwd(_has_qmd, tmp_path):
    session_project = tmp_path / "user-project"
    (session_project / "src").mkdir(parents=True)
    absolute = str(session_project / "src" / "authMiddleware.ts")

    result = build_tool_context("Read", absolute, "/somewhere/else", "s1")

    assert result.metadata["file_path"] == os.path.realpath(absolute)


@patch("memento.lifecycle.has_qmd", return_value=False)
def test_tool_context_relative_path_without_cwd_uses_process_cwd(_has_qmd, tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    monkeypatch.chdir(project)

    result = build_tool_context("Read", "src/authMiddleware.ts", "", "s1")

    assert result.metadata["file_path"] == os.path.realpath(str(project / "src" / "authMiddleware.ts"))


def test_load_cache_drops_pre_schema_dir_entries(tmp_path, monkeypatch):
    import memento.lifecycle as lifecycle_module

    cache_file = tmp_path / "tool-context-cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "dirs": {"/foreign/docs": {"results": [{"path": "notes/pi-session-candidate-capture-3.md"}]}},
                "last_qmd_call": 123.0,
                "injections": {"s1": {"count": 2, "paths": ["notes/a.md"]}},
            }
        )
    )
    monkeypatch.setattr(lifecycle_module, "CACHE_PATH", str(cache_file))

    cache = lifecycle_module.load_cache()

    # Pre-schema dir entries may be poisoned by the relative-path cwd bug;
    # they are dropped while session injection state survives the migration.
    assert cache["schema"] == lifecycle_module.TOOL_CONTEXT_CACHE_SCHEMA
    assert cache["dirs"] == {}
    assert cache["last_qmd_call"] == 123.0
    assert cache["injections"] == {"s1": {"count": 2, "paths": ["notes/a.md"]}}


def test_load_cache_keeps_current_schema_entries(tmp_path, monkeypatch):
    import memento.lifecycle as lifecycle_module

    cache_file = tmp_path / "tool-context-cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "schema": lifecycle_module.TOOL_CONTEXT_CACHE_SCHEMA,
                "dirs": {"/project/docs": {"results": [{"path": "notes/good.md"}]}},
                "last_qmd_call": 5.0,
                "injections": {},
            }
        )
    )
    monkeypatch.setattr(lifecycle_module, "CACHE_PATH", str(cache_file))

    cache = lifecycle_module.load_cache()

    assert cache["dirs"] == {"/project/docs": {"results": [{"path": "notes/good.md"}]}}


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


def test_triage_health_warning_reads_always_on_health_log(tmp_path):
    health_log = tmp_path / "triage-health.jsonl"
    health_log.write_text(
        "\n".join(
            [
                json.dumps({"ts": "2999-01-01T00:00:00", "hook": "triage", "action": "structured_notes_llm_failed"}),
                json.dumps({"ts": "2999-01-01T00:00:01", "hook": "triage", "action": "parse_transcript_failed"}),
                json.dumps({"ts": "2999-01-01T00:00:02", "hook": "triage", "action": "structured_notes_parse_empty"}),
            ]
        )
        + "\n"
    )

    with patch("memento.lifecycle.TRIAGE_HEALTH_LOG_PATH", str(health_log)):
        warning = triage_health_warning()

    assert warning is not None
    assert "triage failing 3/3" in warning
    assert str(health_log) in warning


def test_triage_health_warning_falls_back_to_legacy_retrieval_log(tmp_path):
    health_log = tmp_path / "missing-triage-health.jsonl"
    retrieval_log = tmp_path / "retrieval.jsonl"
    invalid_mcp_error = (
        "Error: Invalid MCP configuration:\nmcpServers: Does not adhere to MCP server configuration schema"
    )
    retrieval_log.write_text(
        "\n".join(
            [
                json.dumps({"ts": "2999-01-01T00:00:00", "hook": "triage", "action": "decision"}),
                json.dumps(
                    {
                        "ts": "2999-01-01T00:00:01",
                        "hook": "triage",
                        "action": "structured_notes_llm_failed",
                        "error": invalid_mcp_error,
                    }
                ),
                json.dumps({"ts": "2999-01-01T00:00:02", "hook": "triage", "action": "parse_transcript_failed"}),
            ]
        )
        + "\n"
    )

    with (
        patch("memento.lifecycle.TRIAGE_HEALTH_LOG_PATH", str(health_log)),
        patch("memento.lifecycle.RETRIEVAL_LOG_PATH", str(retrieval_log)),
    ):
        warning = triage_health_warning()

    assert warning is not None
    assert "triage failing 2/3" in warning
    assert str(retrieval_log) in warning
    assert "stale headless Claude MCP config" in warning


def test_triage_health_warning_adds_invalid_mcp_hint(tmp_path):
    health_log = tmp_path / "triage-health.jsonl"
    invalid_mcp_error = (
        "Error: Invalid MCP configuration:\nmcpServers: Does not adhere to MCP server configuration schema"
    )
    health_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2999-01-01T00:00:00",
                        "hook": "triage",
                        "action": "structured_notes_llm_failed",
                        "error": invalid_mcp_error,
                    }
                ),
                json.dumps({"ts": "2999-01-01T00:00:01", "hook": "triage", "action": "structured_notes_llm_failed"}),
                json.dumps({"ts": "2999-01-01T00:00:02", "hook": "triage", "action": "structured_notes_parse_empty"}),
            ]
        )
        + "\n"
    )

    with patch("memento.lifecycle.TRIAGE_HEALTH_LOG_PATH", str(health_log)):
        warning = triage_health_warning()

    assert warning is not None
    assert "triage failing 3/3" in warning
    assert "stale headless Claude MCP config" in warning
    assert "./install.sh --reinstall" in warning
    assert '{"mcpServers": {}}' in warning


def test_triage_health_warning_adds_stale_certainty_hint(tmp_path):
    health_log = tmp_path / "triage-health.jsonl"
    certainty_error = "invalid literal for int() with base 10: 'confirmed'"
    health_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2999-01-01T00:00:00",
                        "hook": "triage",
                        "action": "structured_notes_failed",
                        "error": certainty_error,
                    }
                ),
                json.dumps({"ts": "2999-01-01T00:00:01", "hook": "triage", "action": "structured_notes_failed"}),
                json.dumps({"ts": "2999-01-01T00:00:02", "hook": "triage", "action": "structured_notes_parse_empty"}),
            ]
        )
        + "\n"
    )

    with patch("memento.lifecycle.TRIAGE_HEALTH_LOG_PATH", str(health_log)):
        warning = triage_health_warning()

    assert warning is not None
    assert "triage failing 3/3" in warning
    assert "stale installed memento package" in warning
    assert "./install.sh --reinstall" in warning
    assert "certainty labels like confirmed" in warning


def test_triage_health_warning_detects_other_accepted_certainty_labels(tmp_path):
    health_log = tmp_path / "triage-health.jsonl"
    certainty_error = "invalid literal for int() with base 10: 'verified'"
    health_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2999-01-01T00:00:00",
                        "hook": "triage",
                        "action": "structured_notes_failed",
                        "error": certainty_error,
                    }
                ),
                json.dumps({"ts": "2999-01-01T00:00:01", "hook": "triage", "action": "structured_notes_failed"}),
                json.dumps({"ts": "2999-01-01T00:00:02", "hook": "triage", "action": "structured_notes_parse_empty"}),
            ]
        )
        + "\n"
    )

    with patch("memento.lifecycle.TRIAGE_HEALTH_LOG_PATH", str(health_log)):
        warning = triage_health_warning()

    assert warning is not None
    assert "stale installed memento package" in warning
    assert "./install.sh --reinstall" in warning


class TestDeferredWorkerResolution:
    def _make_layout(self, root, installed):
        """Create a fake lifecycle.py location plus a worker script.

        installed=False: <root>/memento/lifecycle.py + <root>/hooks/<worker>
        installed=True:  <root>/memento/lifecycle.py + <root>/<worker>  (the
        ~/.claude/hooks layout install.sh produces)
        """
        (root / "memento").mkdir(parents=True)
        fake_lifecycle = root / "memento" / "lifecycle.py"
        fake_lifecycle.write_text("# fake\n")
        if installed:
            worker = root / "vault-briefing.py"
        else:
            (root / "hooks").mkdir()
            worker = root / "hooks" / "vault-briefing.py"
        worker.write_text("# worker\n")
        return fake_lifecycle, worker

    def test_find_hook_script_repo_layout(self, tmp_path, monkeypatch):
        import memento.lifecycle as lifecycle_module

        fake_lifecycle, worker = self._make_layout(tmp_path / "repo", installed=False)
        monkeypatch.setattr(lifecycle_module, "__file__", str(fake_lifecycle))

        assert lifecycle_module._find_hook_script("vault-briefing.py") == worker

    def test_find_hook_script_installed_layout(self, tmp_path, monkeypatch):
        import memento.lifecycle as lifecycle_module

        fake_lifecycle, worker = self._make_layout(tmp_path / "claude-hooks", installed=True)
        monkeypatch.setattr(lifecycle_module, "__file__", str(fake_lifecycle))

        assert lifecycle_module._find_hook_script("vault-briefing.py") == worker

    def test_find_hook_script_missing_returns_none(self, tmp_path, monkeypatch):
        import memento.lifecycle as lifecycle_module

        (tmp_path / "empty" / "memento").mkdir(parents=True)
        fake_lifecycle = tmp_path / "empty" / "memento" / "lifecycle.py"
        fake_lifecycle.write_text("# fake\n")
        monkeypatch.setattr(lifecycle_module, "__file__", str(fake_lifecycle))

        assert lifecycle_module._find_hook_script("vault-briefing.py") is None

    def test_spawn_deferred_search_uses_installed_layout_worker(self, tmp_path, monkeypatch):
        import memento.lifecycle as lifecycle_module

        fake_lifecycle, worker = self._make_layout(tmp_path / "claude-hooks", installed=True)
        monkeypatch.setattr(lifecycle_module, "__file__", str(fake_lifecycle))
        monkeypatch.setattr(lifecycle_module, "DEFERRED_BRIEFING_PATH", str(tmp_path / "deferred.json"))

        with patch("memento.lifecycle._subprocess.Popen") as mock_popen:
            lifecycle_module.spawn_deferred_search("api-service", "main", [], {})

        cmd = mock_popen.call_args[0][0]
        assert cmd[1] == str(worker)
        assert Path(cmd[1]).exists()
        assert (tmp_path / "deferred.json").exists()

    def test_spawn_deferred_search_missing_worker_logs_and_skips(self, tmp_path, monkeypatch):
        import memento.lifecycle as lifecycle_module

        (tmp_path / "empty" / "memento").mkdir(parents=True)
        fake_lifecycle = tmp_path / "empty" / "memento" / "lifecycle.py"
        fake_lifecycle.write_text("# fake\n")
        monkeypatch.setattr(lifecycle_module, "__file__", str(fake_lifecycle))
        monkeypatch.setattr(lifecycle_module, "DEFERRED_BRIEFING_PATH", str(tmp_path / "deferred.json"))

        with (
            patch("memento.lifecycle._subprocess.Popen") as mock_popen,
            patch("memento.lifecycle.log_retrieval") as mock_log,
        ):
            lifecycle_module.spawn_deferred_search("api-service", "main", [], {})

        mock_popen.assert_not_called()
        # No stale pending file is left behind for recall to wait on.
        assert not (tmp_path / "deferred.json").exists()
        mock_log.assert_called_once()
        assert mock_log.call_args[0] == ("briefing", "deferred-worker-missing")

    def test_spawn_deep_recall_uses_installed_layout_worker(self, tmp_path, monkeypatch):
        import memento.lifecycle as lifecycle_module

        root = tmp_path / "claude-hooks"
        (root / "memento").mkdir(parents=True)
        fake_lifecycle = root / "memento" / "lifecycle.py"
        fake_lifecycle.write_text("# fake\n")
        worker = root / "vault-recall.py"
        worker.write_text("# worker\n")
        monkeypatch.setattr(lifecycle_module, "__file__", str(fake_lifecycle))
        monkeypatch.setattr(lifecycle_module, "RUNTIME_DIR", str(tmp_path))
        monkeypatch.setattr(lifecycle_module, "DEEP_RECALL_PENDING_PATH", str(tmp_path / "pending.json"))

        with patch("memento.lifecycle._subprocess.Popen") as mock_popen:
            lifecycle_module.spawn_deep_recall("why does the cache fail?", [], {})

        cmd = mock_popen.call_args[0][0]
        assert cmd[1] == str(worker)
        assert Path(cmd[1]).exists()

    def test_spawn_deep_recall_missing_worker_logs_and_skips(self, tmp_path, monkeypatch):
        import memento.lifecycle as lifecycle_module

        (tmp_path / "empty" / "memento").mkdir(parents=True)
        fake_lifecycle = tmp_path / "empty" / "memento" / "lifecycle.py"
        fake_lifecycle.write_text("# fake\n")
        monkeypatch.setattr(lifecycle_module, "__file__", str(fake_lifecycle))
        monkeypatch.setattr(lifecycle_module, "DEEP_RECALL_PENDING_PATH", str(tmp_path / "pending.json"))

        with (
            patch("memento.lifecycle._subprocess.Popen") as mock_popen,
            patch("memento.lifecycle.log_retrieval") as mock_log,
        ):
            lifecycle_module.spawn_deep_recall("why does the cache fail?", [], {})

        mock_popen.assert_not_called()
        assert not (tmp_path / "pending.json").exists()
        mock_log.assert_called_once()
        assert mock_log.call_args[0] == ("recall", "deep-recall-worker-missing")


class TestTriageWarnRateLimitAndErrorText:
    def _failing_log(self, tmp_path):
        health_log = tmp_path / "triage-health.jsonl"
        health_log.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "ts": "2999-01-01T00:00:00",
                            "hook": "triage",
                            "action": "structured_notes_llm_failed",
                            "error": "Prompt is too long",
                        }
                    ),
                    json.dumps(
                        {"ts": "2999-01-01T00:00:01", "hook": "triage", "action": "structured_notes_llm_failed"}
                    ),
                    json.dumps(
                        {"ts": "2999-01-01T00:00:02", "hook": "triage", "action": "structured_notes_parse_empty"}
                    ),
                ]
            )
            + "\n"
        )
        return health_log

    def test_warning_includes_last_recorded_error_text(self, tmp_path):
        health_log = self._failing_log(tmp_path)

        with patch("memento.lifecycle.TRIAGE_HEALTH_LOG_PATH", str(health_log)):
            warning = triage_health_warning()

        assert warning is not None
        assert 'last error: "Prompt is too long"' in warning

    def test_rate_limited_warning_fires_once_per_day(self, tmp_path):
        health_log = self._failing_log(tmp_path)
        state = tmp_path / "triage-warn-state.json"

        with (
            patch("memento.lifecycle.TRIAGE_HEALTH_LOG_PATH", str(health_log)),
            patch("memento.lifecycle.TRIAGE_WARN_STATE_PATH", str(state)),
        ):
            first = triage_health_warning(rate_limited=True)
            second = triage_health_warning(rate_limited=True)

        assert first is not None
        assert "triage failing" in first
        assert second is None

    def test_rate_limit_resets_on_a_new_day(self, tmp_path):
        health_log = self._failing_log(tmp_path)
        state = tmp_path / "triage-warn-state.json"
        state.write_text(json.dumps({"date": "2001-01-01"}))

        with (
            patch("memento.lifecycle.TRIAGE_HEALTH_LOG_PATH", str(health_log)),
            patch("memento.lifecycle.TRIAGE_WARN_STATE_PATH", str(state)),
        ):
            warning = triage_health_warning(rate_limited=True)

        assert warning is not None
        assert json.loads(state.read_text())["date"] != "2001-01-01"

    def test_diagnostic_surface_is_never_rate_limited(self, tmp_path):
        health_log = self._failing_log(tmp_path)
        state = tmp_path / "triage-warn-state.json"

        with (
            patch("memento.lifecycle.TRIAGE_HEALTH_LOG_PATH", str(health_log)),
            patch("memento.lifecycle.TRIAGE_WARN_STATE_PATH", str(state)),
        ):
            triage_health_warning(rate_limited=True)
            unlimited = triage_health_warning()

        assert unlimited is not None
        assert "triage failing" in unlimited

    def test_healthy_log_writes_no_rate_limit_state(self, tmp_path):
        health_log = tmp_path / "triage-health.jsonl"
        health_log.write_text(
            json.dumps({"ts": "2999-01-01T00:00:00", "hook": "triage", "action": "structured_notes_written"}) + "\n"
        )
        state = tmp_path / "triage-warn-state.json"

        with (
            patch("memento.lifecycle.TRIAGE_HEALTH_LOG_PATH", str(health_log)),
            patch("memento.lifecycle.TRIAGE_WARN_STATE_PATH", str(state)),
        ):
            warning = triage_health_warning(rate_limited=True)

        assert warning is None
        assert not state.exists()
