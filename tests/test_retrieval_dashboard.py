from __future__ import annotations

import json
from pathlib import Path

from memento import retrieval_dashboard


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


def test_main_writes_html_dashboard(tmp_path, capsys, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(vault, "debug-surface", date="2026-06-01T00:00:00Z", certainty=5, project="repo")

    retrieval_log = tmp_path / "retrieval.jsonl"
    triage_health_log = tmp_path / "triage-health.jsonl"
    retrieval_log.write_text("\n".join(json.dumps(entry) for entry in _sample_entries()) + "\n", encoding="utf-8")
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
