"""Tests for smart-store duplicate and supersession suggestions."""

from pathlib import Path

from memento.smart_store import suggest_store_action, write_smart_store_note
from memento.store import write_note


def _seed_note(vault: Path, *, title: str, body: str, tags: list[str] | None = None) -> str:
    result = write_note(
        vault,
        title=title,
        body=body,
        note_type="discovery",
        tags=tags or ["sync"],
        certainty=4,
        source="mcp",
        project="/home/vic/Projects/memento-vault",
        branch="main",
    )
    return str(result.relative_to(vault))


def test_exact_duplicate_is_reported_as_already_covered(tmp_vault, monkeypatch):
    monkeypatch.setattr("memento.smart_store.get_vault", lambda: tmp_vault)
    monkeypatch.setattr("memento.smart_store.has_qmd", lambda: False)
    monkeypatch.setattr("memento.smart_store.inspect_contradictions", lambda *args, **kwargs: {})

    existing_path = _seed_note(tmp_vault, title="Duplicate safe note", body="Same body.")

    result = suggest_store_action(
        title="Duplicate safe note",
        body="Same body.",
        tags=["sync"],
        certainty=4,
        project="/home/vic/Projects/memento-vault",
        branch="main",
    )

    assert result["decision"] == "already_covered"
    assert result["path"] == existing_path
    assert result["created"] is False


def test_near_duplicate_is_reported_as_candidate_update(tmp_vault, monkeypatch):
    monkeypatch.setattr("memento.smart_store.get_vault", lambda: tmp_vault)
    monkeypatch.setattr("memento.smart_store.has_qmd", lambda: False)
    monkeypatch.setattr("memento.smart_store.inspect_contradictions", lambda *args, **kwargs: {})

    existing_path = _seed_note(tmp_vault, title="Redis cache guidance", body="Use the shared cache for shard reads.")

    result = suggest_store_action(
        title="Redis cache guidance",
        body="Use the shared cache for shard reads. Add the TTL defaults and rollout note.",
        tags=["sync"],
        certainty=4,
        project="/home/vic/Projects/memento-vault",
        branch="main",
    )

    assert result["decision"] == "candidate_update"
    assert result["path"] == existing_path
    assert result["best_candidate"]["path"] == existing_path


def test_supersession_cue_is_reported(tmp_vault, monkeypatch):
    monkeypatch.setattr("memento.smart_store.get_vault", lambda: tmp_vault)
    monkeypatch.setattr("memento.smart_store.has_qmd", lambda: False)
    monkeypatch.setattr("memento.smart_store.inspect_contradictions", lambda *args, **kwargs: {})

    existing_path = _seed_note(tmp_vault, title="Redis cache guidance", body="Use the shared cache for all shards.")

    result = suggest_store_action(
        title="Redis cache guidance",
        body="Replace the shared cache guidance: no longer use the shared cache; switch to shard-specific TTL.",
        tags=["sync"],
        certainty=4,
        project="/home/vic/Projects/memento-vault",
        branch="main",
    )

    assert result["decision"] == "supersedes_suggested"
    assert result["path"] == existing_path
    assert result["best_candidate"]["path"] == existing_path


def test_write_smart_store_note_creates_when_no_close_match(tmp_vault, monkeypatch):
    monkeypatch.setattr("memento.smart_store.get_vault", lambda: tmp_vault)
    monkeypatch.setattr("memento.smart_store.has_qmd", lambda: False)
    monkeypatch.setattr("memento.smart_store.inspect_contradictions", lambda *args, **kwargs: {})

    result = write_smart_store_note(
        title="Fresh note",
        body="An entirely new idea.",
        tags=["new"],
    )

    assert result["decision"] == "created"
    assert result["created"] is True
    assert result["path"]
    assert (tmp_vault / result["path"]).exists()
