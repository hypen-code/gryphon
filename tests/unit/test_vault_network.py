"""Dynamic authentication tests using only injected HTTP and DNS doubles."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from gryphon.errors import ConfigurationError, ExecutionError, SecurityViolationError
from gryphon.models import BasicAuthConfig, KeycloakAuthConfig, OAuth2AuthConfig, SessionAuthConfig, StaticAuthConfig
from gryphon.security.auth import AsyncVault
from gryphon.security.network import NetworkClient
from gryphon.security.vault import resolve_auth_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from gryphon.config import GryphonConfig

_TOKEN_ENDPOINT = "https://auth.example.com/token"
_MOCK_TOKEN_RESPONSE = {"access_token": "mocked-token", "expires_in": 3600, "token_type": "Bearer"}
_LOGIN_URL = "https://app.example.com/api/login"
_LOGIN_URL_FORM = "https://app.example.com/login"
_KC_BASE = "https://keycloak.example.com/auth"
_KC_REALM = "myrealm"
_KC_TOKEN_URL = f"{_KC_BASE}/realms/{_KC_REALM}/protocol/openid-connect/token"


async def _resolver(host: str, port: int) -> list[str]:
    """Return a deterministic public IP without touching DNS."""
    return ["93.184.216.34"]


@pytest.fixture
async def auth_network(
    gryphon_config: GryphonConfig,
) -> AsyncIterator[tuple[AsyncVault, list[httpx.Request], dict[str, Any]]]:
    """Provide a mutable fake response and a broker-local credential resolver."""
    requests: list[httpx.Request] = []
    state: dict[str, Any] = {"status": 200, "json": _MOCK_TOKEN_RESPONSE, "headers": {}}

    def handle(request: httpx.Request) -> httpx.Response:
        """Capture outbound host-only credentials and return configured content."""
        requests.append(request)
        return httpx.Response(state["status"], json=state["json"], headers=state["headers"])

    network = NetworkClient(gryphon_config, resolver=_resolver, transport=httpx.MockTransport(handle))
    vault = AsyncVault(network)
    try:
        yield vault, requests, state
    finally:
        vault.clear()
        await network.close()


# ---------------------------------------------------------------------------
# resolve_auth_config — OAuth2 token fetch (mocked)
# ---------------------------------------------------------------------------


async def test_oauth2_fetches_token(auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]]) -> None:
    vault, requests, _ = auth_network
    auth = OAuth2AuthConfig(token_url=_TOKEN_ENDPOINT, client_id="cid", client_secret="sec")
    assert await vault.resolve("svc", auth) == {"Authorization": "Bearer mocked-token"}
    assert requests[0].headers["host"] == "auth.example.com"


async def test_oauth2_cache_hit(auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]]) -> None:
    vault, requests, _ = auth_network
    auth = OAuth2AuthConfig(token_url=_TOKEN_ENDPOINT, client_id="cid", client_secret="sec")
    await vault.resolve("svc", auth)
    await vault.resolve("svc", auth)
    # Token endpoint must NOT have been called a second time
    assert len(requests) == 1


async def test_oauth2_cache_expired(auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]]) -> None:
    vault, requests, _ = auth_network
    auth = OAuth2AuthConfig(token_url=_TOKEN_ENDPOINT, client_id="cid", client_secret="sec")
    await vault.resolve("svc", auth)
    key = next(iter(vault._cache))
    vault._cache[key] = ({"Authorization": "Bearer old-token"}, time.monotonic() - 1)  # already expired
    await vault.resolve("svc", auth)
    assert len(requests) == 2


async def test_oauth2_config_change_invalidates_cache(
    auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]],
) -> None:
    vault, requests, _ = auth_network
    auth = OAuth2AuthConfig(token_url=_TOKEN_ENDPOINT, client_id="cid", client_secret="sec")
    await vault.resolve("svc", auth)
    await vault.resolve("svc", auth.model_copy(update={"client_id": "other"}))
    assert len(requests) == 2


async def test_oauth2_error_sanitized(auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]]) -> None:
    vault, _, state = auth_network
    state.update(status=401, json={"error": "sensitive upstream data"})
    auth = OAuth2AuthConfig(token_url=_TOKEN_ENDPOINT, client_id="cid", client_secret="sec")
    with pytest.raises(ExecutionError, match="^Upstream returned HTTP 401$"):
        await vault.resolve("svc", auth)


# ---------------------------------------------------------------------------
# resolve_auth_config — Keycloak token URL construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["", "/"])
async def test_keycloak_builds_token_url(
    auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]],
    suffix: str,
) -> None:
    vault, requests, _ = auth_network
    auth = KeycloakAuthConfig(
        base_url=_KC_BASE + suffix,  # optional trailing slash
        realm=_KC_REALM,
        client_id="gryphon",
        client_secret="sec",
    )
    await vault.resolve("svc", auth)
    assert requests[0].url.path == httpx.URL(_KC_TOKEN_URL).path


# ---------------------------------------------------------------------------
# Session auth — cookie-based login (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cookie_name", ["", "JSESSIONID"])
async def test_session_cookie_auth(
    auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]],
    cookie_name: str,
) -> None:
    vault, _, state = auth_network
    state.update(json={}, headers={"Set-Cookie": "JSESSIONID=abc123; Path=/"})
    auth = SessionAuthConfig(login_url=_LOGIN_URL, username="u", password="p", cookie_name=cookie_name)
    assert await vault.resolve("svc", auth) == {"Cookie": "JSESSIONID=abc123"}


async def test_session_token_field(auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]]) -> None:
    vault, _, _ = auth_network
    auth = SessionAuthConfig(login_url=_LOGIN_URL, username="u", password="p", token_field="access_token")
    assert await vault.resolve("svc", auth) == {"Authorization": "Bearer mocked-token"}


async def test_session_form_encoding(auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]]) -> None:
    vault, requests, _ = auth_network
    auth = SessionAuthConfig(
        login_url=_LOGIN_URL_FORM,
        username="u",
        password="p",
        content_type="form",
        token_field="access_token",
    )
    await vault.resolve("svc", auth)
    assert requests[0].headers["content-type"] == "application/x-www-form-urlencoded"


async def test_session_cache_hit(auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]]) -> None:
    vault, requests, _ = auth_network
    auth = SessionAuthConfig(login_url=_LOGIN_URL, username="u", password="p", token_field="access_token")
    await vault.resolve("svc", auth)
    await vault.resolve("svc", auth)
    assert len(requests) == 1


async def test_session_cache_expired_refetches(
    auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]],
) -> None:
    vault, requests, _ = auth_network
    auth = SessionAuthConfig(
        login_url=_LOGIN_URL,
        username="u",
        password="p",
        token_field="access_token",
        expires_seconds=1,
    )
    await vault.resolve("svc", auth)
    await vault.resolve("svc", auth)
    assert len(requests) == 2


async def test_session_failure_sanitized(auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]]) -> None:
    vault, _, state = auth_network
    state.update(status=401, json={"error": "bad credentials"})
    with pytest.raises(ExecutionError, match="^Upstream returned HTTP 401$"):
        await vault.resolve("svc", SessionAuthConfig(login_url=_LOGIN_URL, username="u", password="p"))


@pytest.mark.parametrize("options", [{"token_field": "missing"}, {"cookie_name": "missing"}, {}])
async def test_session_missing_auth_data_rejected(
    auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]],
    options: dict[str, str],
) -> None:
    vault, _, _ = auth_network
    auth = SessionAuthConfig.model_validate({"login_url": _LOGIN_URL, "username": "u", "password": "p", **options})
    with pytest.raises(ExecutionError, match="Authentication"):
        await vault.resolve("svc", auth)


async def test_async_legacy_dynamic_api_fails_explicitly() -> None:
    with pytest.raises(ConfigurationError, match="AsyncVault"):
        resolve_auth_config("svc", OAuth2AuthConfig(token_url=_TOKEN_ENDPOINT, client_id="c", client_secret="s"))


async def test_private_login_url_is_blocked(gryphon_config: GryphonConfig) -> None:
    async def private(host: str, port: int) -> list[str]:
        """Return loopback without DNS."""
        return ["127.0.0.1"]

    network = NetworkClient(gryphon_config, resolver=private)
    try:
        with pytest.raises(SecurityViolationError, match="network policy"):
            await AsyncVault(network).resolve(
                "svc",
                OAuth2AuthConfig(
                    token_url=_TOKEN_ENDPOINT,
                    client_id="c",
                    client_secret="s",
                ),
            )
    finally:
        await network.close()


async def test_missing_secret_fails_closed(
    auth_network: tuple[AsyncVault, list[httpx.Request], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GRYPHON_MISSING_TEST", raising=False)
    vault, _, _ = auth_network
    with pytest.raises(ConfigurationError, match="resolved"):
        await vault.resolve("svc", StaticAuthConfig(value="Bearer ${GRYPHON_MISSING_TEST}"))


def test_basic_missing_secret_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRYPHON_MISSING_TEST", raising=False)
    with pytest.raises(ConfigurationError, match="resolved"):
        resolve_auth_config("svc", BasicAuthConfig(username="u", password="${GRYPHON_MISSING_TEST}"))
