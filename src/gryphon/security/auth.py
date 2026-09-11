"""Broker-local asynchronous authentication with revocable, host-only credentials."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from gryphon.errors import ConfigurationError, ExecutionError
from gryphon.models import (
    BasicAuthConfig,
    JwtAuthConfig,
    KeycloakAuthConfig,
    OAuth2AuthConfig,
    SessionAuthConfig,
    StaticAuthConfig,
)
from gryphon.security.network import decode_json
from gryphon.security.vault import resolve_auth_config, resolve_broker_env_headers, resolve_env_references

if TYPE_CHECKING:
    from gryphon.models import AuthConfig
    from gryphon.security.network import NetworkClient

_CACHE_SKEW_SECONDS = 30
_MAX_AUTH_BYTES = 65536
_MAX_AUTH_HEADERS = 128


class AsyncVault:
    """Resolve all supported auth types using one broker's validated network client."""

    def __init__(self, network: NetworkClient) -> None:
        """Create a private credential cache and serialize concurrent refreshes."""
        self._network = network
        self._cache: dict[str, tuple[dict[str, str], float]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def resolve(self, server_name: str, auth: AuthConfig | None) -> dict[str, str]:
        """Return trusted HTTP headers, resolving dynamic credentials only on the host.

        Args:
            server_name: Validated registry identifier.
            auth: Administrator-supplied auth config, or legacy host environment.

        Returns:
            Credential headers, never environment variables for a sandbox.
        """
        if self._closed:
            raise ConfigurationError("Authentication authority has been closed")
        if auth is None:
            return _checked_headers(resolve_broker_env_headers(server_name))
        extras = _checked_headers(resolve_broker_env_headers(server_name, include_credentials=False))
        if isinstance(auth, StaticAuthConfig | JwtAuthConfig | BasicAuthConfig):
            return _checked_headers({**extras, "Authorization": resolve_auth_config(server_name, auth)})
        key = hashlib.sha256((server_name + auth.model_dump_json()).encode()).hexdigest()
        async with self._lock:
            if self._closed:
                raise ConfigurationError("Authentication authority has been closed")
            cached = self._cache.get(key)
            if cached and time.monotonic() < cached[1] - _CACHE_SKEW_SECONDS:
                return _checked_headers({**extras, **cached[0]})
            if isinstance(auth, SessionAuthConfig):
                headers, lifetime = await self._session(auth)
            else:
                headers, lifetime = await self._oauth(auth)
            if self._closed:
                raise ConfigurationError("Authentication authority has been closed")
            headers = _checked_headers(headers)
            combined = _checked_headers({**extras, **headers})
            self._cache[key] = (headers, time.monotonic() + lifetime)
            return combined

    async def _oauth(self, auth: OAuth2AuthConfig | KeycloakAuthConfig) -> tuple[dict[str, str], float]:
        """Fetch a client-credentials token without retries or global caching."""
        if isinstance(auth, KeycloakAuthConfig):
            if auth.realm in {"", ".", ".."} or any(char in auth.realm for char in "/\\%?#"):
                raise ConfigurationError("Invalid authentication realm")
            url = f"{auth.base_url.rstrip('/')}/realms/{quote(auth.realm, safe='')}/protocol/openid-connect/token"
        else:
            url = auth.token_url
        basic = BasicAuthConfig(
            username=quote(_secret(auth.client_id), safe=""),
            password=quote(_secret(auth.client_secret), safe=""),
        )
        headers = _checked_headers({"Authorization": resolve_auth_config("", basic)})
        data = {"grant_type": "client_credentials"}
        if auth.scope:
            data["scope"] = auth.scope
        response = await self._network.request("POST", url, headers=headers, data=data, max_bytes=_MAX_AUTH_BYTES)
        payload = decode_json(response)
        if not isinstance(payload, dict):
            raise ExecutionError("Authentication returned an invalid token response")
        token = _token(payload.get("access_token"))
        lifetime = _lifetime(payload.get("expires_in", 3600))
        return {"Authorization": f"Bearer {token}"}, lifetime

    async def _session(self, auth: SessionAuthConfig) -> tuple[dict[str, str], float]:
        """Perform a bounded login and extract only configured tokens or cookies."""
        payload = {auth.username_field: _secret(auth.username), auth.password_field: _secret(auth.password)}
        response = await self._network.request(
            "POST",
            auth.login_url,
            data=payload if auth.content_type == "form" else None,
            json_body=payload if auth.content_type == "json" else None,
            max_bytes=_MAX_AUTH_BYTES,
        )
        if auth.token_field:
            body = decode_json(response)
            if not isinstance(body, dict) or auth.token_field not in body:
                raise ExecutionError("Authentication token field not found in response")
            headers = {"Authorization": "Bearer " + _token(body[auth.token_field])}
        else:
            try:
                cookies = dict(response.cookies)
            except httpx.CookieConflict:
                raise ExecutionError("Authentication returned ambiguous cookies") from None
            if auth.cookie_name:
                if not cookies.get(auth.cookie_name):
                    raise ExecutionError("Authentication cookie not set in login response")
                cookies = {auth.cookie_name: cookies[auth.cookie_name]}
            if not cookies:
                raise ExecutionError("Authentication returned no cookies")
            headers = {"Cookie": "; ".join(f"{key}={value}" for key, value in cookies.items())}
        return headers, _lifetime(auth.expires_seconds)

    def close(self) -> None:
        """Revoke resolution and ensure in-flight logins cannot repopulate caches."""
        self._closed = True
        self.clear()

    def clear(self) -> None:
        """Discard all cached credential values when the broker closes."""
        self._cache.clear()


def _checked_headers(headers: dict[str, str]) -> dict[str, str]:
    """Bound credentials and reject ambiguous case aliases, injection and placeholders."""
    if (
        len(headers) > _MAX_AUTH_HEADERS
        or sum(len(key) + len(value) for key, value in headers.items()) > _MAX_AUTH_BYTES
    ):
        raise ConfigurationError("Authentication headers exceed supported limits")
    if len({name.lower() for name in headers}) != len(headers):
        raise ConfigurationError("Authentication headers are ambiguous")
    for value in headers.values():
        if "${" in value or any(ord(char) < 32 or ord(char) >= 127 for char in value):
            raise ConfigurationError("Authentication configuration could not be resolved safely")
    return headers


def _secret(value: str) -> str:
    """Expand host-only secret references, failing closed on unresolved placeholders."""
    resolved = resolve_env_references(value)
    if "${" in resolved:
        raise ConfigurationError("Authentication configuration could not be resolved")
    return resolved


def _token(value: Any) -> str:
    """Validate a token without reflecting its value in an error."""
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ExecutionError("Authentication returned an invalid token")
    return value


def _lifetime(value: Any) -> float:
    """Validate finite positive authentication cache TTLs."""
    try:
        lifetime = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ExecutionError("Authentication returned an invalid expiration") from None
    if not math.isfinite(lifetime) or lifetime <= 0:
        raise ExecutionError("Authentication returned an invalid expiration")
    return lifetime
