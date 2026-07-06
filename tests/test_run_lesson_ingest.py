"""Integration tests for the MEM-145 ``pi_bridge run-lesson`` ingest command.

Before this command existed, run evidence from an external runner (Rondo, or
an OLI-17-style specimen run) had no deterministic path into the vault: it was
either dropped entirely or queued outside the vault where it was not
recallable. These tests exercise the whole path end to end against a real,
hermetic vault and the real grep search backend (no qmd binary, no embedding
model, no network) to prove the "deterministic integration check" the ticket
asks for: ingest a sample payload, then confirm search by the run id and by
the ticket id each return the produced note.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memento import pi_bridge
from memento.config import DEFAULT_CONFIG


@pytest.fixture
def hermetic_vault_config(tmp_vault, monkeypatch):
    """Point the whole config/search-backend singleton chain at tmp_vault.

    ``search_backend: grep`` keeps this hermetic: no external qmd process and
    no embedding model download, just the built-in GrepBackend fallback that
    reads real markdown files under the vault.
    """
    config = dict(DEFAULT_CONFIG)
    config["vault_path"] = str(tmp_vault)
    config["search_backend"] = "grep"
    monkeypatch.setattr("memento.config._CONFIG", config)
    monkeypatch.setattr("memento.search_backend._backend", None)
    return tmp_vault


def _write_payload(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run_lesson(payload_path: Path, capsys) -> dict:
    code = pi_bridge.main(["run-lesson", "--payload", str(payload_path)])
    assert code == 0
    return json.loads(capsys.readouterr().out)


def _search(query: str, capsys) -> dict:
    code = pi_bridge.main(["search", "--query", query])
    assert code == 0
    return json.loads(capsys.readouterr().out)


def test_run_lesson_ingest_creates_note_recallable_by_run_id_and_ticket_id(hermetic_vault_config, tmp_path, capsys):
    run_id = "RON-160-20260705T031413Z-d846d0b8"
    ticket_id = "MEM-145"
    payload_path = _write_payload(
        tmp_path,
        {
            "run_id": run_id,
            "ticket_id": ticket_id,
            "title": "OLI-17 specimen run: queue path was silently dropped",
            "lesson_text": "The specimen run finished but nothing was queued or recallable afterward.",
            "evidence_paths": ["rondo://runs/RON-160/proof-summary"],
            "tags": ["oli-17", "ingest"],
        },
    )

    result = _run_lesson(payload_path, capsys)

    assert result.get("error") is None, result
    assert result["queued"] is False
    assert result["created"] is True
    note_path = hermetic_vault_config / result["path"]
    assert note_path.exists()
    content = note_path.read_text(encoding="utf-8")
    assert run_id in content
    assert ticket_id in content
    assert "oli-17" in content

    by_run_id = _search(run_id, capsys)
    assert any(r["path"] == result["path"] for r in by_run_id["results"]), by_run_id

    by_ticket_id = _search(ticket_id, capsys)
    assert any(r["path"] == result["path"] for r in by_ticket_id["results"]), by_ticket_id


def test_run_lesson_ingest_derives_title_and_lesson_text_when_omitted(hermetic_vault_config, tmp_path, capsys):
    run_id = "RON-161-20260706T090000Z-abc12345"
    ticket_id = "MEM-999"
    payload_path = _write_payload(tmp_path, {"run_id": run_id, "ticket_id": ticket_id})

    result = _run_lesson(payload_path, capsys)

    assert result["created"] is True
    content = (hermetic_vault_config / result["path"]).read_text(encoding="utf-8")
    assert run_id in content
    assert ticket_id in content

    by_run_id = _search(run_id, capsys)
    assert any(r["path"] == result["path"] for r in by_run_id["results"])
    by_ticket_id = _search(ticket_id, capsys)
    assert any(r["path"] == result["path"] for r in by_ticket_id["results"])


def test_run_lesson_ingest_requires_run_id_and_ticket_id(hermetic_vault_config, tmp_path, capsys):
    payload_path = _write_payload(tmp_path, {"title": "No identifiers"})

    result = _run_lesson(payload_path, capsys)

    assert result["reason"] == "missing_required_field"
    assert "run_id" in result["error"]
    assert "ticket_id" in result["error"]


def test_run_lesson_ingest_rejects_missing_payload_file(hermetic_vault_config, tmp_path, capsys):
    missing = tmp_path / "does-not-exist.json"

    result = _run_lesson(missing, capsys)

    assert result["reason"] == "payload_read_error"


def test_run_lesson_ingest_rejects_invalid_json(hermetic_vault_config, tmp_path, capsys):
    payload_path = tmp_path / "payload.json"
    payload_path.write_text("{not json", encoding="utf-8")

    result = _run_lesson(payload_path, capsys)

    assert result["reason"] == "payload_invalid_json"


def test_run_lesson_ingest_json_serializable_when_vault_has_token_overlap(hermetic_vault_config, tmp_path, capsys):
    """MEM-168: a pre-existing note sharing tokens must not crash the JSON response.

    ``_combine_candidate_sources`` falls back to ``find_dedup_candidates``
    (title/tag token overlap) whenever qmd/embeddings are unavailable, exactly
    the hermetic setup these tests use. Once the vault already contains any
    note that shares a token with the new note's title, ``suggest_store_action``
    builds a dedup-candidate row carrying a raw Python ``set`` under
    ``"tokens"`` and forwards it into the JSON payload emitted by
    ``pi_bridge._run_json``. Before the fix, ``json.dumps`` inside ``_emit``
    raises ``TypeError: Object of type set is not JSON serializable``, which
    ``_run_json``'s catch-all then reports as a generic ``{"error": ...}``
    even though the note was already written to disk.
    """
    existing_note = hermetic_vault_config / "notes" / "oli-17-earlier-context.md"
    existing_note.write_text(
        "\n".join(
            [
                "---",
                "title: OLI-17 specimen run earlier context",
                "type: discovery",
                "tags: [oli-17]",
                "date: 2026-07-01T00:00",
                "certainty: 3",
                "---",
                "",
                "Earlier context for the OLI-17 specimen run investigation.",
                "",
                "## Related",
                "",
            ]
        ),
        encoding="utf-8",
    )

    run_id = "RON-168-20260706T000000Z-deadbeef"
    ticket_id = "MEM-168"
    payload_path = _write_payload(
        tmp_path,
        {
            "run_id": run_id,
            "ticket_id": ticket_id,
            "title": "OLI-17 specimen run: dedup candidate token overlap",
            "lesson_text": "New evidence unrelated in substance to the earlier note.",
            "tags": ["oli-17"],
        },
    )

    result = _run_lesson(payload_path, capsys)

    assert result.get("error") is None, result
    assert result["created"] is True
    assert result.get("path"), result
    note_path = hermetic_vault_config / result["path"]
    assert note_path.exists()


def test_run_lesson_ingest_rejects_patch_blob_lesson_text(hermetic_vault_config, tmp_path, capsys):
    """A raw diff smuggled through the mapped lesson_text field is still rejected.

    The CLI payload only maps the 6 contract fields into the internal
    candidate, but whatever lands in ``body``/``evidence_summary`` still goes
    through the same unsafe-shape rejection the MCP tool uses, so a patch
    blob pasted into lesson_text never reaches the vault.
    """
    payload_path = _write_payload(
        tmp_path,
        {
            "run_id": "RON-162",
            "ticket_id": "MEM-145",
            "lesson_text": "diff --git a/a b/a\n@@ -1 +1 @@\n-old\n+new",
        },
    )

    result = _run_lesson(payload_path, capsys)

    assert result["reason"] == "invalid_automated_run_lesson"
