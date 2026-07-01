from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from memento import retrieval_dashboard


def test_load_jsonl_normalizes_offset_aware_timestamps(tmp_path):
    path = tmp_path / "retrieval.jsonl"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now.replace(year=now.year - 1)
    path.write_text(
        "\n".join(
            [
                json.dumps({"ts": old.isoformat(), "action": "old"}),
                json.dumps({"ts": now.isoformat().replace("+00:00", "Z"), "action": "zulu"}),
                json.dumps({"ts": now.astimezone(timezone.utc).isoformat(), "action": "aware"}),
                json.dumps({"ts": now.replace(tzinfo=None).isoformat(), "action": "naive"}),
            ]
        )
        + "\n"
    )

    loaded = retrieval_dashboard._load_jsonl(path, since_days=1)

    assert [entry["action"] for entry in loaded] == ["zulu", "aware", "naive"]


def _write_note(vault: Path, name: str, *, date: str, certainty: int, project: str, body: str = "body") -> Path:
    notes_dir = vault / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_path = notes_dir / f"{name}.md"
    note_path.write_text(
        f"---\ndate: {date}\ncertainty: {certainty}\nproject: {project}\n---\n{body}\n",
        encoding="utf-8",
    )
    return note_path


def _sample_entries() -> list[dict[str, object]]:
    return [
        {
            "ts": "2026-06-29T10:00:00Z",
            "hook": "recall",
            "action": "inject",
            "query": "fix the token ghp_abcdefghijklmnopqrstuvwxyz0123456789AB and the sidebar dashboard",
            "latency_ms": 42,
            "results_before": 5,
            "results_after": 3,
            "injected_titles": ["Debug surface"],
            "injected_chars": 88,
            "pipeline": "bm25+prf+ce",
            "multi_hop_gate": True,
            "multi_hop_added": 1,
            "deep_recall_spawned": False,
            "top_path": "notes/debug-surface.md",
        },
        {
            "ts": "2026-06-29T10:00:01Z",
            "hook": "recall",
            "action": "dedup-skip",
            "query": "sidebar dashboard",  # should stay hidden unless explicitly requested
        },
        {
            "ts": "2026-06-29T10:00:02Z",
            "hook": "tool-context",
            "action": "decision",
            "decision": "injected",
            "source": "search",
            "query": "open the app py token ghp_abcdefghijklmnopqrstuvwxyz0123456789AB",
            "latency_ms": 15,
            "injected_paths": ["notes/debug-surface.md"],
            "injected_titles": ["Debug surface"],
            "injected_chars": 42,
            "candidates": [
                {"path": "notes/debug-surface.md", "title": "Debug surface", "score": 0.92, "decision": "candidate"},
                {"path": "notes/other.md", "title": "Other note", "score": 0.12, "decision": "candidate"},
            ],
        },
        {
            "ts": "2026-06-29T10:00:03Z",
            "hook": "briefing",
            "action": "inject",
            "latency_ms": 21,
        },
        {
            "ts": "2026-06-29T10:00:04Z",
            "hook": "triage",
            "action": "decision",
            "substantial": True,
            "new_insight": False,
            "exchanges": 5,
            "agent_spawned": True,
            "project": "repo",
        },
        {
            "ts": "2026-06-29T10:00:05Z",
            "hook": "inception",
            "action": "trigger",
            "new_notes": 3,
            "threshold": 2,
        },
    ]


def _recommendation_entries() -> list[dict[str, object]]:
    return [
        {"ts": "2026-06-29T10:01:00Z", "hook": "recall", "action": "no-results", "query": "memento.yml"},
        {
            "ts": "2026-06-29T10:01:01Z",
            "hook": "mcp",
            "action": "search_miss",
            "reason": "no-results",
            "query": "hooks/vault-tool-context.py",
        },
        {
            "ts": "2026-06-29T10:01:02Z",
            "hook": "search",
            "action": "project_match_required",
            "reason": "project-mismatch-filtered-empty",
            "query": "pyproject.toml and workflow.md",
            "project": "alpha",
        },
        {
            "ts": "2026-06-29T10:01:03Z",
            "hook": "recall",
            "action": "threshold_too_high",
            "query": "README.md",
            "project": "alpha",
        },
        {
            "ts": "2026-06-29T10:01:04Z",
            "hook": "recall",
            "action": "project-mismatch-filtered-empty",
            "query": "docs/frontmatter-schema.md",
            "project": "alpha",
        },
        {
            "ts": "2026-06-29T10:01:05Z",
            "hook": "recall",
            "action": "no-results",
            "query": "what happened in project history for release planning",
        },
        {
            "ts": "2026-06-29T10:01:06Z",
            "hook": "recall",
            "action": "no-results",
            "query": "catch me up on project history",
        },
        {
            "ts": "2026-06-29T10:01:07Z",
            "hook": "recall",
            "action": "no-results",
            "query": "what changed and why",
        },
        {
            "ts": "2026-06-29T10:01:08Z",
            "hook": "recall",
            "action": "no-results",
            "query": "summarize the project history",
        },
        {
            "ts": "2026-06-29T10:01:09Z",
            "hook": "tool-context",
            "action": "decision",
            "decision": "no-results",
            "dir_key": "memento/search",
            "file_path": "memento/search.py",
        },
        {
            "ts": "2026-06-29T10:01:09Z",
            "hook": "tool-context",
            "action": "decision",
            "decision": "no-results",
            "dir_key": "memento/search",
            "file_path": "memento/search.py",
        },
        {
            "ts": "2026-06-29T10:01:10Z",
            "hook": "tool-context",
            "action": "decision",
            "decision": "no-results",
            "dir_key": "memento/search",
            "file_path": "memento/search.py",
        },
        {
            "ts": "2026-06-29T10:01:11Z",
            "hook": "recall",
            "action": "inject",
            "query": "deep prompt one",
            "latency_ms": 310,
            "pipeline": "bm25+prf+ce",
        },
        {
            "ts": "2026-06-29T10:01:12Z",
            "hook": "recall",
            "action": "inject",
            "query": "deep prompt two",
            "latency_ms": 280,
            "pipeline": "bm25+prf+ce",
        },
        {
            "ts": "2026-06-29T10:01:13Z",
            "hook": "recall",
            "action": "inject",
            "query": "deep prompt three",
            "latency_ms": 295,
            "pipeline": "bm25+prf+ce",
        },
    ]


def _access_followup_entries() -> list[dict[str, object]]:
    return [
        {
            "ts": "2026-06-29T10:02:00Z",
            "path": "notes/detail-defaults.md",
            "hook": "mcp",
            "tool": "search",
            "rank": 1,
            "query_hash": "q1",
            "query_summary": "how should detail_level work for secret ghp_abcdefghijklmnopqrstuvwxyz0123456789AB",
            "result_count": 1,
        },
        {
            "ts": "2026-06-29T10:02:20Z",
            "path": "notes/detail-defaults.md",
            "hook": "mcp",
            "tool": "get",
            "rank": 1,
            "query_summary": "notes/detail-defaults.md",
            "result_count": 1,
        },
        {
            "ts": "2026-06-29T10:03:00Z",
            "path": "notes/detail-defaults.md",
            "hook": "mcp",
            "tool": "search",
            "rank": 1,
            "query_hash": "q2",
            "query_summary": "need full content for retrieval dashboard",
            "result_count": 1,
        },
        {
            "ts": "2026-06-29T10:03:30Z",
            "path": "notes/detail-defaults.md",
            "hook": "mcp",
            "tool": "get",
            "rank": 1,
            "query_summary": "notes/detail-defaults.md",
            "result_count": 1,
        },
        {
            "ts": "2026-06-29T10:04:00Z",
            "path": "notes/other.md",
            "hook": "mcp",
            "tool": "search",
            "rank": 1,
            "query_hash": "q3",
            "query_summary": "show other note body",
            "result_count": 1,
        },
        {
            "ts": "2026-06-29T10:04:30Z",
            "path": "notes/other.md",
            "hook": "mcp",
            "tool": "get",
            "rank": 1,
            "query_summary": "notes/other.md",
            "result_count": 1,
        },
    ]


def test_build_report_redacts_queries_and_enriches_notes(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(vault, "debug-surface", date="2026-06-01T00:00:00Z", certainty=5, project="repo")
    _write_note(vault, "other", date="2026-05-20T00:00:00Z", certainty=2, project="repo")

    entries = _sample_entries()
    access_stats = {
        "notes/debug-surface.md": {
            "events": [{"ts": "2026-06-28T10:00:00Z", "rank": 1}, {"ts": "2026-06-29T10:00:00Z", "rank": 1}]
        },
        "notes/other.md": {"events": [{"ts": "2026-06-10T10:00:00Z", "rank": 1}]},
    }
    triage_health_entries = [
        {"ts": "2026-06-29T09:59:00Z", "hook": "triage", "action": "structured_notes_written", "notes_written": 2},
        {"ts": "2026-06-29T09:59:30Z", "hook": "triage", "action": "structured_notes_llm_failed", "error": "boom"},
    ]

    report = retrieval_dashboard.build_report(
        entries,
        vault_path=vault,
        access_stats=access_stats,
        triage_health_entries=triage_health_entries,
        include_sensitive=False,
        event_limit=10,
        note_limit=5,
    )

    text = retrieval_dashboard.render_text_report(report)
    html = retrieval_dashboard.render_html_report(report)

    assert "ghp_supersecret" not in text
    assert "ghp_supersecret" not in html
    assert "sidebar dashboard" not in text
    assert "sidebar dashboard" not in html
    assert "query_digest=" in text
    assert "query_digest" in html
    assert "query_preview" not in html

    assert report["tool_context"]["injected"] == 1
    assert report["tool_context"]["candidate_snapshots"][0]["path"] == "notes/debug-surface.md"
    assert report["top_notes"][0]["path"] == "notes/debug-surface.md"
    assert report["top_notes"][0]["access_count"] == 2
    assert report["top_notes"][0]["certainty"] == 5
    assert report["triage_health"]["failures"] == 1
    assert report["triage_health"]["last_failure"]["action"] == "structured_notes_llm_failed"

    sensitive_report = retrieval_dashboard.build_report(
        entries,
        vault_path=vault,
        access_stats=access_stats,
        triage_health_entries=triage_health_entries,
        include_sensitive=True,
        event_limit=10,
        note_limit=5,
    )
    sensitive_text = retrieval_dashboard.render_text_report(sensitive_report)
    assert "sidebar dashboard" in sensitive_text
    assert "ghp_supersecret" not in sensitive_text


def test_build_report_adds_behavior_recommendations_without_leaking_queries(tmp_path):
    report = retrieval_dashboard.build_report(
        _recommendation_entries(),
        vault_path=tmp_path,
        access_entries=_access_followup_entries(),
        include_sensitive=False,
        event_limit=20,
        note_limit=5,
    )

    text = retrieval_dashboard.render_text_report(report)
    html = retrieval_dashboard.render_html_report(report)
    titles = {rec["title"] for rec in report["recommendations"]}

    assert len(report["recommendations"]) >= 4
    assert "Enable or tune concrete search" in titles
    assert "Add a project-history/query tool" in titles
    assert "Lower the recall threshold or improve note tags" in titles
    assert "Add a code-area tool or project map" in titles
    assert "Return fuller search results or tune detail_level" in titles
    assert "Prefer a purpose-built tool for common deep-retrieval prompts" in titles
    assert "memento.yml" not in text
    assert "project history" not in text
    assert "pyproject.toml" not in html
    assert "detail_level work" not in text
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB" not in html
    assert "Recommendations" in text
    assert "Recommendations" in html


def test_retrieval_report_consumes_benchmark_memory_outcomes(tmp_path):
    outcomes = [
        {
            "task_id": "mv-a",
            "memory_used": True,
            "memory_classification": "used_relevant_memory",
            "memory_contribution_measurable": True,
            "retrieval_latency_ms": 120,
            "memory_token_budget": 900,
        },
        {
            "task_id": "mv-b",
            "memory_used": True,
            "memory_classification": "irrelevant_memory",
            "memory_failure_type": "irrelevant_memory",
            "retrieval_latency_ms": 240,
            "memory_token_budget": 1200,
        },
    ]
    outcome_path = tmp_path / "outcomes.jsonl"
    outcome_path.write_text("\n".join(json.dumps(item) for item in outcomes) + "\n", encoding="utf-8")

    loaded = retrieval_dashboard.load_benchmark_outcomes(outcome_path)
    report = retrieval_dashboard.build_report([], vault_path=tmp_path, benchmark_outcomes=loaded)
    text = retrieval_dashboard.render_text_report(report)
    html = retrieval_dashboard.render_html_report(report)

    assert report["benchmark_outcomes"]["count"] == 2
    assert report["benchmark_outcomes"]["memory_used"] == 2
    assert report["benchmark_outcomes"]["memory_contribution_measurable"] == 1
    assert report["benchmark_outcomes"]["failures"] == {"irrelevant_memory": 1}
    assert "Benchmark memory outcomes" in text
    assert "irrelevant_memory: 1" in text
    assert "Benchmark memory outcomes" in html


def test_main_writes_html_dashboard(tmp_path, capsys, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(vault, "debug-surface", date="2026-06-01T00:00:00Z", certainty=5, project="repo")

    retrieval_log = tmp_path / "retrieval.jsonl"
    triage_health_log = tmp_path / "triage-health.jsonl"
    access_log = tmp_path / "access-log.jsonl"
    retrieval_log.write_text("\n".join(json.dumps(entry) for entry in _sample_entries()) + "\n", encoding="utf-8")
    access_log.write_text("", encoding="utf-8")
    triage_health_log.write_text(
        json.dumps(
            {"ts": "2026-06-29T09:59:30Z", "hook": "triage", "action": "structured_notes_llm_failed", "error": "boom"}
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(retrieval_dashboard, "get_vault", lambda: vault)
    monkeypatch.setattr(
        retrieval_dashboard,
        "load_access_log_stats",
        lambda: {"notes/debug-surface.md": {"events": [{"ts": "2026-06-29T10:00:00Z", "rank": 1}]}},
    )

    output = tmp_path / "dashboard.html"
    code = retrieval_dashboard.main(
        [
            "--retrieval-log",
            str(retrieval_log),
            "--triage-health-log",
            str(triage_health_log),
            "--access-log",
            str(access_log),
            "--since",
            "30",
            "--html",
            "--output",
            str(output),
            "--limit",
            "10",
            "--note-limit",
            "5",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == str(output)
    assert output.exists()
    rendered = output.read_text(encoding="utf-8")
    assert "Memento retrieval debug report" in rendered
    assert "debug-surface.md" in rendered
