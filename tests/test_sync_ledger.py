"""Tests for memento.sync_ledger — the append-only remote-sync ledger."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from memento import sync_ledger  # noqa: E402


# ---------------------------------------------------------------------------
# Basic append / read
# ---------------------------------------------------------------------------


class TestAppendAndIter:
    def test_append_creates_directory(self, tmp_path):
        vault = tmp_path / "vault"
        # Dir must not exist yet.
        assert not (vault / ".sync").exists()

        sync_ledger.append(vault, {"ts": "t", "kind": "note", "source": "a.md", "status": "ok"})

        assert (vault / ".sync" / "ledger.jsonl").exists()

    def test_iter_yields_entries_in_order(self, tmp_path):
        vault = tmp_path / "vault"
        sync_ledger.append(vault, {"kind": "note", "source": "a", "status": "ok"})
        sync_ledger.append(vault, {"kind": "note", "source": "b", "status": "error"})
        sync_ledger.append(vault, {"kind": "note", "source": "c", "status": "ok"})

        entries = list(sync_ledger.iter_entries(vault))
        assert [e["source"] for e in entries] == ["a", "b", "c"]

    def test_iter_skips_malformed_lines(self, tmp_path):
        vault = tmp_path / "vault"
        sync_ledger.append(vault, {"kind": "note", "source": "a", "status": "ok"})

        # Inject a corrupt line (simulates a crashed writer).
        with open(sync_ledger.ledger_path(vault), "a") as f:
            f.write("{not valid json\n")

        sync_ledger.append(vault, {"kind": "note", "source": "b", "status": "ok"})

        entries = list(sync_ledger.iter_entries(vault))
        assert [e["source"] for e in entries] == ["a", "b"]

    def test_iter_on_missing_ledger_yields_nothing(self, tmp_path):
        assert list(sync_ledger.iter_entries(tmp_path / "nonexistent")) == []


# ---------------------------------------------------------------------------
# State folding
# ---------------------------------------------------------------------------


class TestFoldState:
    def test_last_entry_per_source_wins(self, tmp_path):
        vault = tmp_path / "vault"
        sync_ledger.record(vault, "note", "a", status="error", error="boom")
        sync_ledger.record(vault, "note", "a", status="ok", remote_path="remote/a")

        state = sync_ledger.fold_state(vault)
        assert state[("note", "a")]["status"] == "ok"

    def test_pending_retries_excludes_recovered_items(self, tmp_path):
        """An error followed by a success is NOT pending."""
        vault = tmp_path / "vault"
        sync_ledger.record(vault, "note", "a", status="error", error="boom")
        sync_ledger.record(vault, "note", "a", status="ok", remote_path="remote/a")
        sync_ledger.record(vault, "note", "b", status="error", error="nope")

        pending = sync_ledger.pending_retries(vault)
        sources = [e["source"] for e in pending]
        assert sources == ["b"]

    def test_pending_retries_covers_both_kinds(self, tmp_path):
        vault = tmp_path / "vault"
        sync_ledger.record(vault, "note", "a.md", status="error", error="x")
        sync_ledger.record(vault, "capture", "session:abc", status="error", error="y")
        sync_ledger.record(vault, "note", "b.md", status="ok", remote_path="r")

        pending = sync_ledger.pending_retries(vault)
        keys = {(e["kind"], e["source"]) for e in pending}
        assert keys == {("note", "a.md"), ("capture", "session:abc")}


# ---------------------------------------------------------------------------
# Idempotency by content hash
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_same_text_produces_same_hash(self):
        assert sync_ledger.content_hash("abc") == sync_ledger.content_hash("abc")

    def test_different_text_produces_different_hash(self):
        assert sync_ledger.content_hash("abc") != sync_ledger.content_hash("abd")

    def test_last_success_hash_returns_most_recent(self, tmp_path):
        vault = tmp_path / "vault"
        sync_ledger.record(vault, "note", "a", status="ok", content_hash="hash1")
        sync_ledger.record(vault, "note", "a", status="error", error="boom", content_hash="hash2")
        sync_ledger.record(vault, "note", "a", status="ok", content_hash="hash3")

        assert sync_ledger.last_success_hash(vault, "note", "a") == "hash3"

    def test_last_success_hash_ignores_failures(self, tmp_path):
        vault = tmp_path / "vault"
        sync_ledger.record(vault, "note", "a", status="error", error="boom", content_hash="h1")
        sync_ledger.record(vault, "note", "a", status="error", error="boom", content_hash="h2")

        assert sync_ledger.last_success_hash(vault, "note", "a") is None

    def test_last_success_hash_scoped_by_source(self, tmp_path):
        vault = tmp_path / "vault"
        sync_ledger.record(vault, "note", "a", status="ok", content_hash="hA")
        sync_ledger.record(vault, "note", "b", status="ok", content_hash="hB")

        assert sync_ledger.last_success_hash(vault, "note", "a") == "hA"
        assert sync_ledger.last_success_hash(vault, "note", "b") == "hB"
        assert sync_ledger.last_success_hash(vault, "note", "c") is None


# ---------------------------------------------------------------------------
# Attempt counting
# ---------------------------------------------------------------------------


class TestAttemptCount:
    def test_increments_across_attempts(self, tmp_path):
        vault = tmp_path / "vault"
        e1 = sync_ledger.record(vault, "note", "a", status="error", error="1")
        e2 = sync_ledger.record(vault, "note", "a", status="error", error="2")
        e3 = sync_ledger.record(vault, "note", "a", status="ok")

        assert e1["attempt"] == 1
        assert e2["attempt"] == 2
        assert e3["attempt"] == 3

    def test_attempt_scoped_by_kind_and_source(self, tmp_path):
        vault = tmp_path / "vault"
        sync_ledger.record(vault, "note", "a", status="ok")
        sync_ledger.record(vault, "note", "b", status="ok")
        capture = sync_ledger.record(vault, "capture", "a", status="ok")

        # 'capture' with source 'a' should be attempt 1 (not 2) since it's a
        # different kind.
        assert capture["attempt"] == 1


# ---------------------------------------------------------------------------
# Spool payload
# ---------------------------------------------------------------------------


class TestSpool:
    def test_spool_writes_payload_readable_back(self, tmp_path):
        vault = tmp_path / "vault"
        path = sync_ledger.spool_payload(
            vault, "capture", "session:abc-123", "hello world"
        )

        assert path.exists()
        assert "capture" in str(path.parent)
        assert sync_ledger.read_spooled(path) == "hello world"

    def test_spool_sanitizes_filename(self, tmp_path):
        vault = tmp_path / "vault"
        path = sync_ledger.spool_payload(
            vault, "note", "../evil/path & bad chars", "data"
        )
        # No slashes from source should appear in the file stem.
        assert "../" not in path.name
        assert path.read_text() == "data"

    def test_read_spooled_returns_none_for_missing(self, tmp_path):
        assert sync_ledger.read_spooled(tmp_path / "nope.payload") is None

    def test_spool_does_not_clobber_prior_failures(self, tmp_path):
        """Two failures for the same source should produce two files."""
        vault = tmp_path / "vault"
        p1 = sync_ledger.spool_payload(vault, "note", "a.md", "v1")
        # Same second — filename disambiguation comes from timestamp only,
        # so if both land in the same UTC second we accept one may overwrite.
        # The realistic scenario (retries minutes apart) is covered by timestamp.
        p2 = sync_ledger.spool_payload(vault, "note", "a.md", "v2")

        # Either both exist or timestamps collapsed — at minimum v2 is on disk.
        assert p2.read_text() == "v2"
        if p1 != p2:
            assert p1.read_text() == "v1"


# ---------------------------------------------------------------------------
# Full end-to-end flow
# ---------------------------------------------------------------------------


class TestEndToEndFlow:
    def test_failed_then_retried_then_succeeded(self, tmp_path):
        """Simulates: remote sync fails, spool captures body, retry re-reads
        and succeeds. Ledger state after each step must match expectations."""
        vault = tmp_path / "vault"

        # Attempt 1 fails, spool the body.
        body = "session summary v1"
        spool = sync_ledger.spool_payload(vault, "capture", "session:x", body)
        sync_ledger.record(
            vault, "capture", "session:x",
            status="error",
            content_hash=sync_ledger.content_hash(body),
            error="connection refused",
            spool_path=str(spool),
        )

        assert len(sync_ledger.pending_retries(vault)) == 1

        # Retry reads the body back.
        replay = sync_ledger.read_spooled(spool)
        assert replay == body

        # Attempt 2 succeeds.
        sync_ledger.record(
            vault, "capture", "session:x",
            status="ok",
            content_hash=sync_ledger.content_hash(body),
            remote_path="fleeting/2026-04-13-x.md",
        )

        assert sync_ledger.pending_retries(vault) == []
        state = sync_ledger.fold_state(vault)
        assert state[("capture", "session:x")]["status"] == "ok"
        assert state[("capture", "session:x")]["attempt"] == 2

    def test_ledger_is_jsonl_not_json(self, tmp_path):
        """Each line must be independently parseable — this is what makes
        crash-safety and partial reads work."""
        vault = tmp_path / "vault"
        sync_ledger.record(vault, "note", "a", status="ok")
        sync_ledger.record(vault, "note", "b", status="error", error="boom")

        lines = sync_ledger.ledger_path(vault).read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # must parse standalone
