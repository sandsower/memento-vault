"""Authentication middleware for the memento vault server.

Provides a pluggable auth system that returns an Identity for each request.
Currently supports bearer token auth and no-auth (local mode).
Designed to be extended with per-user tokens, JWT, or OAuth later.

Integrates with MCP's TokenVerifier protocol for HTTP transport auth.
"""

import os
from dataclasses import dataclass, field
from abc import ABC, abstractmethod


@dataclass(frozen=True)
class Identity:
    """Represents an authenticated caller."""

    id: str
    name: str
    roles: tuple[str, ...] = field(default=("owner",))

    @property
    def is_owner(self) -> bool:
        return "owner" in self.roles


# Singleton identity for single-owner vaults
VAULT_OWNER = Identity(id="vault-owner", name="Vault Owner", roles=("owner",))


class AuthProvider(ABC):
    """Abstract authentication provider."""

    @abstractmethod
    def authenticate(self, token: str | None) -> Identity | None:
        """Authenticate a request and return an Identity, or None if rejected."""
        ...


class NoAuth(AuthProvider):
    """No authentication — always returns the vault owner. Used in local/stdio mode."""

    def authenticate(self, token: str | None) -> Identity:
        return VAULT_OWNER


class BearerTokenAuth(AuthProvider):
    """Single shared bearer token authentication.

    All authenticated requests get the vault owner identity.
    Designed to be extended later: map different tokens to different users.
    """

    def __init__(self, expected_token: str):
        if not expected_token:
            raise ValueError("BearerTokenAuth requires a non-empty token")
        self._expected_token = expected_token

    def authenticate(self, token: str | None) -> Identity | None:
        if not token:
            return None
        if token.startswith("Bearer "):
            token = token[7:]
        if token == self._expected_token:
            return VAULT_OWNER
        return None


class MementoTokenVerifier:
    """MCP-compatible TokenVerifier that wraps our AuthProvider.

    Implements the TokenVerifier protocol expected by FastMCP's
    token_verifier parameter for HTTP transport auth.
    """

    def __init__(self, auth_provider: AuthProvider):
        self._auth = auth_provider

    async def verify_token(self, token: str):
        """Verify a bearer token. Returns AccessToken or None."""
        from mcp.server.auth.provider import AccessToken

        identity = self._auth.authenticate(token)
        if identity is None:
            return None
        return AccessToken(
            token=token,
            client_id=identity.id,
            scopes=list(identity.roles),
        )


def create_auth_provider(config: dict | None = None) -> AuthProvider:
    """Create an auth provider based on configuration.

    If MEMENTO_API_KEY env var or config['api_key'] is set, uses BearerTokenAuth.
    Otherwise, falls back to NoAuth (local mode).
    """
    if config is None:
        from memento.config import get_config

        config = get_config()

    api_key = os.environ.get("MEMENTO_API_KEY") or config.get("api_key")
    if api_key:
        return BearerTokenAuth(api_key)
    return NoAuth()
