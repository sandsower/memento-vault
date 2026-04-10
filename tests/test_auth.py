"""Tests for the authentication middleware."""

import pytest
from memento.auth import (
    BearerTokenAuth,
    Identity,
    NoAuth,
    VAULT_OWNER,
    create_auth_provider,
)


class TestIdentity:
    def test_vault_owner_is_owner(self):
        assert VAULT_OWNER.is_owner
        assert VAULT_OWNER.id == "vault-owner"

    def test_custom_identity(self):
        ident = Identity(id="user-1", name="Test User", roles=("reader",))
        assert not ident.is_owner
        assert ident.id == "user-1"

    def test_frozen(self):
        with pytest.raises(AttributeError):
            VAULT_OWNER.id = "hacked"


class TestNoAuth:
    def test_always_returns_owner(self):
        auth = NoAuth()
        assert auth.authenticate(None) == VAULT_OWNER
        assert auth.authenticate("anything") == VAULT_OWNER
        assert auth.authenticate("") == VAULT_OWNER


class TestBearerTokenAuth:
    def test_valid_token(self):
        auth = BearerTokenAuth("secret-key-123")
        identity = auth.authenticate("secret-key-123")
        assert identity is not None
        assert identity.is_owner

    def test_valid_token_with_bearer_prefix(self):
        auth = BearerTokenAuth("secret-key-123")
        identity = auth.authenticate("Bearer secret-key-123")
        assert identity is not None
        assert identity.is_owner

    def test_invalid_token(self):
        auth = BearerTokenAuth("secret-key-123")
        assert auth.authenticate("wrong-key") is None

    def test_empty_token(self):
        auth = BearerTokenAuth("secret-key-123")
        assert auth.authenticate(None) is None
        assert auth.authenticate("") is None

    def test_rejects_empty_expected_token(self):
        with pytest.raises(ValueError):
            BearerTokenAuth("")


class TestCreateAuthProvider:
    def test_no_key_returns_noauth(self):
        provider = create_auth_provider(config={})
        assert isinstance(provider, NoAuth)

    def test_config_key_returns_bearer(self):
        provider = create_auth_provider(config={"api_key": "test-key"})
        assert isinstance(provider, BearerTokenAuth)

    def test_env_key_overrides(self, monkeypatch):
        monkeypatch.setenv("MEMENTO_API_KEY", "env-key")
        provider = create_auth_provider(config={})
        assert isinstance(provider, BearerTokenAuth)
        assert provider.authenticate("env-key") is not None

    def test_env_key_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("MEMENTO_API_KEY", "env-key")
        provider = create_auth_provider(config={"api_key": "config-key"})
        assert provider.authenticate("env-key") is not None
        assert provider.authenticate("config-key") is None
