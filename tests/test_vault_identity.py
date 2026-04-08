"""Tests for vault identity generation and persistence."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from memento.config import get_vault_id, _vault_identity_path


class TestVaultIdentity:
    def test_generates_uuid(self, tmp_path):
        identity_path = tmp_path / "vault-identity.json"
        with patch("memento.config._vault_identity_path", return_value=identity_path):
            vault_id = get_vault_id()
            assert vault_id
            assert len(vault_id) == 32  # hex UUID without dashes

    def test_persists_to_disk(self, tmp_path):
        identity_path = tmp_path / "vault-identity.json"
        with patch("memento.config._vault_identity_path", return_value=identity_path):
            vault_id = get_vault_id()
            assert identity_path.exists()
            data = json.loads(identity_path.read_text())
            assert data["vault_id"] == vault_id
            assert "created" in data

    def test_returns_same_id_on_subsequent_calls(self, tmp_path):
        identity_path = tmp_path / "vault-identity.json"
        with patch("memento.config._vault_identity_path", return_value=identity_path):
            id1 = get_vault_id()
            id2 = get_vault_id()
            assert id1 == id2

    def test_survives_corrupt_file(self, tmp_path):
        identity_path = tmp_path / "vault-identity.json"
        identity_path.write_text("not json{{{")
        with patch("memento.config._vault_identity_path", return_value=identity_path):
            vault_id = get_vault_id()
            assert vault_id
            assert len(vault_id) == 32

    def test_survives_missing_vault_id_key(self, tmp_path):
        identity_path = tmp_path / "vault-identity.json"
        identity_path.write_text(json.dumps({"other": "data"}))
        with patch("memento.config._vault_identity_path", return_value=identity_path):
            vault_id = get_vault_id()
            assert vault_id
            assert len(vault_id) == 32

    def test_creates_parent_directories(self, tmp_path):
        identity_path = tmp_path / "deep" / "nested" / "vault-identity.json"
        with patch("memento.config._vault_identity_path", return_value=identity_path):
            vault_id = get_vault_id()
            assert identity_path.exists()
