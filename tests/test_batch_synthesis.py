from memento.batch_synthesis import synthesize_failure_batch


def test_rejects_raw_run_store_or_log_dump_fields():
    for field in ("stdout", "output", "raw_output", "console", "terminal", "traceback", "stacktrace"):
        result = synthesize_failure_batch(
            {
                "summaries": [
                    {
                        "run_id": "run-1",
                        "summary": "pytest failed",
                        field: "raw output should not enter the vault",
                    }
                ]
            }
        )

        assert result["reason"] == "invalid_summary_schema"
        assert "raw run store" in result["error"]
        assert result["path"] == f"summaries[0].{field}"


def test_rejects_oversized_multiline_summary_strings_as_raw_dumps():
    result = synthesize_failure_batch(
        [
            {
                "run_id": "run-1",
                "summary": "line\n" * 100,
                "failures": [{"signal": "pytest failed"}],
            }
        ]
    )

    assert result["reason"] == "invalid_summary_schema"
    assert "compact" in result["error"]


def test_groups_required_failure_categories_and_candidate_actions():
    result = synthesize_failure_batch(
        [
            {
                "run_id": "r1",
                "summary": "recall missed a prior note and pytest gate failed",
                "failures": [
                    {"signal": "Memento recall did not retrieve the relevant note"},
                    {"signal": "pytest gate failed", "command": "pytest tests/test_store.py"},
                ],
            },
            {
                "run_id": "r2",
                "summary": "same pytest gate failed again",
                "failures": [{"signal": "pytest gate failed", "command": "pytest tests/test_store.py"}],
            },
            {
                "run_id": "r3",
                "summary": "agent ignored instruction, harness timed out, venv missing, requirements ambiguous",
                "failures": [
                    {"signal": "Agent ignored the requested file boundary"},
                    {"signal": "Rondo harness timed out before collecting result"},
                    {"signal": "Environment missing venv dependency"},
                    {"signal": "Acceptance criteria were ambiguous"},
                ],
            },
        ]
    )

    assert result["dry_run"] is True
    assert result["failure_count"] == 7
    assert result["category_counts"] == {
        "memory": 1,
        "process": 2,
        "agent": 1,
        "harness": 1,
        "environment": 1,
        "requirement": 1,
    }

    gate_group = next(group for group in result["groups"] if group["failure_type"] == "gate_failure")
    assert gate_group["repeated"] is True
    assert gate_group["occurrence_count"] == 2

    action_types = {action["type"] for action in result["candidate_actions"]}
    assert {"note", "gate", "issue", "docs"}.issubset(action_types)
    assert result["candidate_lessons"]
    assert all("body" in candidate for candidate in result["candidate_lessons"])
