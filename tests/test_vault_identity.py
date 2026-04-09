"""Tests for vault identity generation and persistence."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from memento.config import get_vault_id, _vault_identity_path, _legacy_vault_identity_path


def _patch_identity(tmp_path):
    """Patch both identity path and legacy path to temp locations."""
    identity_path = tmp_path / "vault" / "vault-identity.json"
    legacy_path = tmp_path / "legacy" / "vault-identity.json"
    return (
        patch("memento.config._vault_identity_path", return_value=identity_path),
        patch("memento.config._legacy_vault_identity_path", return_value=legacy_path),
        identity_path,
        legacy_path,
    )


class TestVaultIdentity:
    def test_generates_uuid(self, tmp_path):
        p1, p2, identity_path, _ = _patch_identity(tmp_path)
        with p1, p2:
            vault_id = get_vault_id()
            assert vault_id
            assert len(vault_id) == 32  # hex UUID without dashes

    def test_persists_to_disk(self, tmp_path):
        p1, p2, identity_path, _ = _patch_identity(tmp_path)
        with p1, p2:
            vault_id = get_vault_id()
            assert identity_path.exists()
            data = json.loads(identity_path.read_text())
            assert data["vault_id"] == vault_id
            assert "created" in data

    def test_returns_same_id_on_subsequent_calls(self, tmp_path):
        p1, p2, identity_path, _ = _patch_identity(tmp_path)
        with p1, p2:
            id1 = get_vault_id()
            id2 = get_vault_id()
            assert id1 == id2

    def test_survives_corrupt_file(self, tmp_path):
        p1, p2, identity_path, _ = _patch_identity(tmp_path)
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_text("not json{{{")
        with p1, p2:
            vault_id = get_vault_id()
            assert vault_id
            assert len(vault_id) == 32

    def test_survives_missing_vault_id_key(self, tmp_path):
        p1, p2, identity_path, _ = _patch_identity(tmp_path)
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_text(json.dumps({"other": "data"}))
        with p1, p2:
            vault_id = get_vault_id()
            assert vault_id
            assert len(vault_id) == 32

    def test_creates_parent_directories(self, tmp_path):
        p1, p2, _, _ = _patch_identity(tmp_path)
        # Override to a deep nested path
        deep_path = tmp_path / "deep" / "nested" / "vault-identity.json"
        with patch("memento.config._vault_identity_path", return_value=deep_path), p2:
            vault_id = get_vault_id()
            assert deep_path.exists()

    def test_migrates_from_legacy_location(self, tmp_path):
        p1, p2, identity_path, legacy_path = _patch_identity(tmp_path)
        # Write identity to legacy location
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(json.dumps({"vault_id": "abc123legacy", "created": "2025-01-01T00:00:00Z"}))
        with p1, p2:
            vault_id = get_vault_id()
            assert vault_id == "abc123legacy"
            # Should now exist in the new location
            assert identity_path.exists()
            data = json.loads(identity_path.read_text())
            assert data["vault_id"] == "abc123legacy"

    def test_two_vaults_get_different_ids(self, tmp_path):
        vault_a = tmp_path / "vault-a" / "vault-identity.json"
        vault_b = tmp_path / "vault-b" / "vault-identity.json"
        legacy = tmp_path / "legacy" / "vault-identity.json"
        lp = patch("memento.config._legacy_vault_identity_path", return_value=legacy)
        with patch("memento.config._vault_identity_path", return_value=vault_a), lp:
            id_a = get_vault_id()
        with patch("memento.config._vault_identity_path", return_value=vault_b), lp:
            id_b = get_vault_id()
        assert id_a != id_b
