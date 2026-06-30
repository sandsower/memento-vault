import json
from pathlib import Path


TASKS_PATH = Path(__file__).resolve().parents[1] / "benchmark/rondo/tasks.json"

REQUIRED_TOP_LEVEL_FIELDS = {
    "schema_version",
    "tasks",
}

REQUIRED_TASK_FIELDS = {
    "id",
    "issue",
    "category",
    "difficulty",
    "requires_vault_knowledge",
    "expected_touched_areas",
    "acceptance_criteria",
    "required_gates",
    "known_risks",
    "retrieval_context",
    "rubric",
}

REQUIRED_ISSUE_FIELDS = {"number", "title", "url", "labels", "body_snapshot"}
REQUIRED_RETRIEVAL_FIELDS = {
    "docs",
    "memento_queries",
    "expected_query_intent",
    "useful_note_refs",
    "memory_token_budget",
}
REQUIRED_RUBRIC_FIELDS = {"success", "agent_failure", "harness_failure", "ambiguous_requirements"}

REQUIRED_CATEGORIES = {
    "retrieval behavior",
    "MCP/tooling",
    "triage/capture",
    "docs/process",
    "sync/security/release",
}

ALLOWED_DIFFICULTIES = {"small", "medium", "large"}


def _load_tasks():
    payload = json.loads(TASKS_PATH.read_text())
    assert REQUIRED_TOP_LEVEL_FIELDS <= set(payload)
    assert payload["schema_version"] == 2
    return payload["tasks"]


def test_rondo_benchmark_task_file_has_required_shape():
    tasks = _load_tasks()

    assert 8 <= len(tasks) <= 12
    for task in tasks:
        assert REQUIRED_TASK_FIELDS <= set(task), task.get("id")
        assert REQUIRED_ISSUE_FIELDS <= set(task["issue"]), task["id"]
        assert REQUIRED_RETRIEVAL_FIELDS <= set(task["retrieval_context"]), task["id"]
        assert REQUIRED_RUBRIC_FIELDS <= set(task["rubric"]), task["id"]
        assert task["id"].startswith("mv-")
        assert task["category"] in REQUIRED_CATEGORIES
        assert task["difficulty"] in ALLOWED_DIFFICULTIES
        assert isinstance(task["issue"]["number"], int)
        assert task["issue"]["body_snapshot"].strip()
        assert task["expected_touched_areas"]
        assert task["acceptance_criteria"]
        assert task["required_gates"]
        assert task["known_risks"]
        assert task["rubric"]["success"]
        assert isinstance(task["retrieval_context"]["expected_query_intent"], str)
        assert task["retrieval_context"]["expected_query_intent"].strip()
        assert isinstance(task["retrieval_context"]["useful_note_refs"], list)
        assert isinstance(task["retrieval_context"]["memory_token_budget"], int)
        if task["requires_vault_knowledge"]:
            assert task["retrieval_context"]["memory_token_budget"] > 0
        else:
            assert task["retrieval_context"]["memory_token_budget"] == 0


def test_rondo_benchmark_tasks_are_unique_and_cover_required_categories():
    tasks = _load_tasks()

    task_ids = [task["id"] for task in tasks]
    issue_numbers = [task["issue"]["number"] for task in tasks]

    assert len(task_ids) == len(set(task_ids))
    assert len(issue_numbers) == len(set(issue_numbers))
    assert REQUIRED_CATEGORIES <= {task["category"] for task in tasks}


def test_rondo_benchmark_includes_vault_knowledge_task():
    tasks = _load_tasks()

    vault_tasks = [task for task in tasks if task["requires_vault_knowledge"]]

    assert vault_tasks
    assert any(task["retrieval_context"]["memento_queries"] for task in vault_tasks)


def test_rondo_benchmark_rubrics_distinguish_failure_modes():
    tasks = _load_tasks()

    for task in tasks:
        rubric = task["rubric"]
        assert rubric["agent_failure"] != rubric["harness_failure"], task["id"]
        assert rubric["ambiguous_requirements"] != rubric["agent_failure"], task["id"]


def test_rondo_benchmark_memory_eval_fields_support_intent_not_exact_ids():
    tasks = _load_tasks()

    vault_tasks = [task for task in tasks if task["requires_vault_knowledge"]]
    assert vault_tasks
    assert any(task["retrieval_context"]["useful_note_refs"] for task in vault_tasks)
    for task in vault_tasks:
        intent = task["retrieval_context"]["expected_query_intent"]
        assert len(intent.split()) >= 6, task["id"]
        assert task["retrieval_context"]["memento_queries"], task["id"]
