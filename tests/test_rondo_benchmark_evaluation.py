import json
import sys
from pathlib import Path

import pytest

ROND0_DIR = Path(__file__).resolve().parents[1] / "benchmark" / "rondo"
sys.path.insert(0, str(ROND0_DIR))

from evaluation import classify_memory_outcome, load_outcomes, load_tasks, summarize_outcomes  # noqa: E402

TASKS_PATH = ROND0_DIR / "tasks.json"


def _task(**overrides):
    task = {
        "id": "mv-memory-task",
        "requires_vault_knowledge": True,
        "retrieval_context": {
            "expected_query_intent": "Recall Pi auto-capture parity decisions for SessionEnd queued capture behavior",
            "useful_note_refs": ["notes/pi-autocapture-parity.md"],
            "memory_token_budget": 1200,
        },
    }
    task.update(overrides)
    return task


def test_classifies_relevant_memory_by_intent_without_exact_note_id():
    result = classify_memory_outcome(
        _task(
            retrieval_context={
                "expected_query_intent": "Recall health diagnostics semantics for retrieval no-results backend unavailable low-signal skips",
                "useful_note_refs": [],
                "memory_token_budget": 1200,
            }
        ),
        {
            "task_id": "mv-memory-task",
            "memory_used": True,
            "retrieval_queries": ["health report retrieval no-results backend unavailable low-signal diagnostics"],
            "retrieved_note_refs": ["notes/some-other-stable-path.md"],
            "retrieval_latency_ms": 87,
            "memory_token_budget": 900,
        },
    )

    assert result["memory_classification"] == "used_relevant_memory"
    assert result["memory_failure_type"] is None
    assert result["memory_contribution_measurable"] is True
    assert result["useful_ref_matches"] == []


def test_classifies_memory_not_retrieved_for_required_vault_task():
    result = classify_memory_outcome(_task(), {"task_id": "mv-memory-task", "memory_used": False})

    assert result["memory_classification"] == "memory_not_retrieved"
    assert result["memory_failure_type"] == "memory_not_retrieved"
    assert result["memory_contribution_measurable"] is False


def test_classifies_irrelevant_memory_when_used_but_intent_and_refs_do_not_match():
    result = classify_memory_outcome(
        _task(),
        {
            "task_id": "mv-memory-task",
            "memory_used": True,
            "retrieval_queries": ["release smoke docker homebrew version metadata"],
            "retrieved_note_refs": ["notes/release-smoke.md"],
        },
    )

    assert result["memory_classification"] == "irrelevant_memory"
    assert result["memory_failure_type"] == "irrelevant_memory"


def test_summarize_outcomes_reports_failure_latency_and_token_budget():
    tasks = {"mv-memory-task": _task()}
    report = summarize_outcomes(
        tasks,
        [
            {
                "task_id": "mv-memory-task",
                "memory_used": True,
                "retrieved_note_refs": ["pi-autocapture-parity"],
                "retrieval_latency_ms": 100,
                "memory_token_budget": 800,
            },
            {"task_id": "mv-memory-task", "memory_used": False, "retrieval_latency_ms": 200},
        ],
    )

    assert report["schema"] == "rondo_benchmark_memory_report/v1"
    assert report["classification_counts"] == {"memory_not_retrieved": 1, "used_relevant_memory": 1}
    assert report["failure_counts"] == {"memory_not_retrieved": 1}
    assert report["latency_ms"]["avg"] == 150.0
    assert report["token_budget"]["avg"] == 1000.0


def test_load_outcomes_accepts_jsonl_stream(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    path.write_text(
        json.dumps({"task_id": "mv-a", "memory_used": True})
        + "\n"
        + json.dumps({"task_id": "mv-b", "memory_used": False})
        + "\n",
        encoding="utf-8",
    )

    assert [item["task_id"] for item in load_outcomes(path)] == ["mv-a", "mv-b"]


def test_load_outcomes_rejects_raw_run_ledgers(tmp_path):
    path = tmp_path / "outcomes.json"
    path.write_text(json.dumps([{"task_id": "mv", "run_ledger": {"events": []}}]), encoding="utf-8")

    with pytest.raises(ValueError, match="raw run store/log fields"):
        load_outcomes(path)


def test_fixture_tasks_can_be_classified_from_minimal_sanitized_outcomes():
    tasks = load_tasks(TASKS_PATH)
    first_vault_task = next(task for task in tasks.values() if task["requires_vault_knowledge"])
    report = summarize_outcomes(
        tasks,
        [
            {
                "task_id": first_vault_task["id"],
                "memory_used": True,
                "retrieval_queries": [first_vault_task["retrieval_context"]["expected_query_intent"]],
                "retrieval_latency_ms": 12,
                "memory_token_budget": first_vault_task["retrieval_context"]["memory_token_budget"],
            }
        ],
    )

    assert report["classified_count"] == 1
    assert report["memory_contribution_measurable_count"] == 1
