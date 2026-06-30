"""Tests for contradiction and supersession inspection."""

from pathlib import Path
from typing import Optional
from unittest.mock import patch

from memento.contradictions import inspect_contradictions


def _write_note(
    path: Path, *, title: str, body: str, certainty: int, date: str, supersedes: Optional[str] = None
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
    lines.extend(["---", "", body, ""])
    path.write_text("\n".join(lines))


def test_inspect_contradictions_marks_supersession_and_opposite_language(tmp_path):
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
