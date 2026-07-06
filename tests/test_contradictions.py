"""Tests for contradiction and supersession inspection."""

from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from memento.config import DEFAULT_CONFIG
from memento.contradictions import apply_invalidation, apply_supersession_backlinks, inspect_contradictions


@pytest.fixture(autouse=True)
def _isolate_vault_write_lock(tmp_path, monkeypatch):
    """apply_supersession_backlinks acquires the vault write lock -- keep it
    off the real ~/.cache/memento-vault so this never races other worktrees'
    concurrent sweeps/tests (same isolation pattern as test_fleeting.py)."""
    monkeypatch.setattr("memento.store.VAULT_WRITE_LOCK_PATH", str(tmp_path / "vault-write.lock"))


def _write_note(
    path: Path,
    *,
    title: str,
    body: str,
    certainty: int,
    date: str,
    supersedes: Optional[str] = None,
    invalidated_by: Optional[str] = None,
    valid_from: Optional[str] = None,
) -> None:
    lines = [
        "---",
        f"title: {title}",
        "type: discovery",
        f"certainty: {certainty}",
        f"date: {date}",
    ]
    if supersedes:
        lines.append(f'supersedes: "[[{supersedes}]]"')
    if invalidated_by:
        lines.append(f"invalidated_by: {invalidated_by}")
    if valid_from:
        lines.append(f"valid_from: {valid_from}")
    lines.extend(["---", "", body, ""])
    path.write_text("\n".join(lines))


def _lexical_config():
    return {**DEFAULT_CONFIG, "contradictions_lexical_fallback": True}


def test_inspect_contradictions_marks_supersession_and_opposite_language(tmp_path):
    """Pre-MEM-163 lexical path, preserved behind contradictions_lexical_fallback."""
    vault = tmp_path / "vault"
    notes_dir = vault / "notes"
    notes_dir.mkdir(parents=True)

    _write_note(
        notes_dir / "redis-cache-v1.md",
        title="Redis cache needs TTL",
        body="Use the shared cache for all shards.",
        certainty=2,
        date="2026-03-01T10:00",
    )
    _write_note(
        notes_dir / "redis-cache-v2.md",
        title="Redis cache should not use the shared cache",
        body="Do not use the shared cache; prefer shard-specific TTL.",
        certainty=4,
        date="2026-03-15T10:00",
        supersedes="redis-cache-v1",
    )
    _write_note(
        notes_dir / "redis-cache-guidance.md",
        title="Redis cache guidance",
        body="Prefer explicit TTL for every cache entry.",
        certainty=3,
        date="2026-03-10T10:00",
    )

    with (
        patch("memento.contradictions.get_vault", return_value=vault),
        patch("memento.contradictions.get_config", return_value=_lexical_config()),
        patch("memento.contradictions.has_qmd", return_value=False),
        patch("memento.graph.get_vault", return_value=vault),
    ):
        payload = inspect_contradictions("redis cache")

    assert payload["topic"] == "redis cache"
    assert payload["results"]

    statuses = {item["path"]: item["status"] for item in payload["results"]}
    assert statuses["notes/redis-cache-v1.md"] == "superseded"
    assert statuses["notes/redis-cache-v2.md"] == "superseding"

    assert payload["contradictions"]
    pair = next(
        item
        for item in payload["contradictions"]
        if {"notes/redis-cache-v1.md", "notes/redis-cache-v2.md"} == set(item["paths"])
    )
    assert pair["kind"] == "opposite-language"
    assert payload["supersession"]
    assert payload["groups"]
    assert "contradiction" in payload["summary"]


def test_inspect_contradictions_defaults_to_validity_chains(tmp_path):
    """MEM-163 default shape: validity chains, not lexical polarity guesses."""
    vault = tmp_path / "vault"
    notes_dir = vault / "notes"
    notes_dir.mkdir(parents=True)

    _write_note(
        notes_dir / "redis-cache-v1.md",
        title="Redis cache needs TTL",
        body="Use the shared cache for all shards.",
        certainty=2,
        date="2026-03-01T10:00",
        invalidated_by="redis-cache-v2",
    )
    _write_note(
        notes_dir / "redis-cache-v2.md",
        title="Redis cache uses shard-specific TTL",
        body="Prefer shard-specific TTL over the shared cache.",
        certainty=4,
        date="2026-03-15T10:00",
        supersedes="redis-cache-v1",
    )
    _write_note(
        notes_dir / "redis-cache-guidance.md",
        title="Redis cache guidance",
        body="Prefer explicit TTL for every cache entry.",
        certainty=3,
        date="2026-03-10T10:00",
    )

    with (
        patch("memento.contradictions.get_vault", return_value=vault),
        patch("memento.contradictions.get_config", return_value=dict(DEFAULT_CONFIG)),
    ):
        payload = inspect_contradictions("redis cache")

    assert payload["topic"] == "redis cache"
    assert "results" not in payload
    assert len(payload["chains"]) == 1

    chain = payload["chains"][0]
    statuses = {node["path"]: node["status"] for node in chain["nodes"]}
    assert statuses["notes/redis-cache-v1.md"] == "invalidated"
    assert statuses["notes/redis-cache-v2.md"] == "current"
    assert chain["current_path"] == "notes/redis-cache-v2.md"

    # oldest to newest ordering
    assert [node["path"] for node in chain["nodes"]] == [
        "notes/redis-cache-v1.md",
        "notes/redis-cache-v2.md",
    ]
    # valid_from defaults to date when absent
    v1 = next(node for node in chain["nodes"] if node["path"] == "notes/redis-cache-v1.md")
    assert v1["valid_from"] == "2026-03-01T10:00"

    assert "invalidated note" in payload["summary"]


def test_inspect_contradictions_empty_topic_is_a_miss(tmp_path):
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    with (
        patch("memento.contradictions.get_vault", return_value=vault),
        patch("memento.contradictions.get_config", return_value=dict(DEFAULT_CONFIG)),
    ):
        payload = inspect_contradictions("   ")
    assert "miss" in payload


def test_apply_invalidation_is_idempotent(tmp_path):
    vault = tmp_path / "vault"
    notes_dir = vault / "notes"
    notes_dir.mkdir(parents=True)
    note_path = notes_dir / "old-note.md"
    _write_note(note_path, title="Old note", body="Body text.", certainty=3, date="2026-01-01T10:00")

    applied = apply_invalidation(vault, "notes/old-note.md", "new-note")
    assert applied is True
    text = note_path.read_text()
    assert "invalidated_by: new-note" in text
    assert "title: Old note" in text
    assert "certainty: 3" in text

    # Second call with the same value is a no-op (idempotent)
    applied_again = apply_invalidation(vault, "notes/old-note.md", "new-note")
    assert applied_again is False


def test_apply_supersession_backlinks_sets_invalidated_by(tmp_path):
    vault = tmp_path / "vault"
    notes_dir = vault / "notes"
    notes_dir.mkdir(parents=True)

    _write_note(notes_dir / "old-note.md", title="Old note", body="Body.", certainty=2, date="2026-01-01T10:00")
    _write_note(
        notes_dir / "new-note.md",
        title="New note",
        body="Body.",
        certainty=4,
        date="2026-02-01T10:00",
        supersedes="old-note",
    )

    report = apply_supersession_backlinks(vault)
    assert report["applied"] == [{"path": "notes/old-note.md", "invalidated_by": "new-note"}]

    text = (notes_dir / "old-note.md").read_text()
    assert "invalidated_by: new-note" in text

    # Idempotent: a second sweep finds it already set, applies nothing new.
    report2 = apply_supersession_backlinks(vault)
    assert report2["applied"] == []
    assert report2["already_set"] == [{"path": "notes/old-note.md", "invalidated_by": "new-note"}]


def test_apply_supersession_backlinks_dry_run_does_not_write(tmp_path):
    vault = tmp_path / "vault"
    notes_dir = vault / "notes"
    notes_dir.mkdir(parents=True)

    _write_note(notes_dir / "old-note.md", title="Old note", body="Body.", certainty=2, date="2026-01-01T10:00")
    _write_note(
        notes_dir / "new-note.md",
        title="New note",
        body="Body.",
        certainty=4,
        date="2026-02-01T10:00",
        supersedes="old-note",
    )

    report = apply_supersession_backlinks(vault, dry_run=True)
    assert report["candidates"] == [{"path": "notes/old-note.md", "invalidated_by": "new-note"}]
    assert report["applied"] == []
    text = (notes_dir / "old-note.md").read_text()
    assert "invalidated_by" not in text


def test_apply_supersession_backlinks_never_overwrites_existing_value(tmp_path):
    vault = tmp_path / "vault"
    notes_dir = vault / "notes"
    notes_dir.mkdir(parents=True)

    _write_note(
        notes_dir / "old-note.md",
        title="Old note",
        body="Body.",
        certainty=2,
        date="2026-01-01T10:00",
        invalidated_by="someone-else",
    )
    _write_note(
        notes_dir / "new-note.md",
        title="New note",
        body="Body.",
        certainty=4,
        date="2026-02-01T10:00",
        supersedes="old-note",
    )

    report = apply_supersession_backlinks(vault)
    assert report["applied"] == []
    assert report["skipped"]
    assert "someone-else" in report["skipped"][0]["reason"]
    text = (notes_dir / "old-note.md").read_text()
    assert "invalidated_by: someone-else" in text
