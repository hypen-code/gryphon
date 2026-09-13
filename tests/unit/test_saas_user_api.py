"""Isolated ASGI account security contracts using disposable SQLite and real password hashing."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import SecretStr
from starlette.requests import Request

from gryphon.models import UserAccount
from gryphon.saas import create_app
from gryphon.saas_access import account
from gryphon.saas_auth import COOKIE_NAME
from gryphon.saas_config import SaaSConfig
from gryphon.saas_passwords import HASH_ADMISSION

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.applications import Starlette

    from gryphon.config import GryphonConfig

PASSWORD = "generated-test-password-42"
NEW_PASSWORD = "replacement-test-password-84"
ORIGIN = "http://127.0.0.1"


@asynccontextmanager
async def _client(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    """Give each browser an independent cookie jar without opening a network port."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        yield client


async def _login(client: httpx.AsyncClient, username: str, password: str = PASSWORD) -> None:
    """Authenticate through the public route and keep only the returned CSRF header."""
    response = await client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200
    assert set(response.json()) == {"csrf_token"}
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/api" in cookie
    client.headers["x-csrf-token"] = response.json()["csrf_token"]


async def _create(client: httpx.AsyncClient, username: str = "administrator") -> UserAccount:
    """Create a persisted platform account using the authenticated bootstrap recovery session."""
    response = await client.post("/api/users", json=_body(username))
    assert response.status_code == 201
    return UserAccount.model_validate(response.json())


def _body(username: str = "administrator") -> dict[str, object]:
    """Build a closed account request containing only disposable test credentials."""
    return {
        "username": username,
        "name": "Private display name",
        "password": PASSWORD,
        "role": "platform_admin",
        "tenant_id": None,
    }


@pytest.fixture
async def api(gryphon_config: GryphonConfig, tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Starlette]]:
    """Initialize real hosted lifespan against temporary SQLite, never ambient operator state."""
    config = SaaSConfig(
        admin_token=SecretStr(secrets.token_urlsafe(48)),
        database_url=SecretStr("sqlite:///" + str(tmp_path / "accounts.db")),
        public_origin=ORIGIN,
        allow_insecure_http=True,
        state_dir=tmp_path / "hosted",
    )
    app = create_app(config, gryphon_config)
    async with app.router.lifespan_context(app), _client(app) as client:
        response = await client.post("/api/login", json={"token": config.admin_token.get_secret_value()})
        assert response.status_code == 200 and set(response.json()) == {"csrf_token"}
        client.headers["x-csrf-token"] = response.json()["csrf_token"]
        yield client, app


async def test_account_bootstrap_persisted_admin_identity_and_self_disable(
    api: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Bootstrap remains recoverable while persisted administrators have verified immutable identities."""
    admin, app = api
    bootstrap = (await admin.get("/api/me")).json()["user"]
    assert bootstrap["id"] is None and bootstrap["role"] == "platform_admin"
    assert set((await admin.get("/api/session")).json()) == {"csrf_token"}
    assert (await admin.post("/api/password", json={})).status_code == 403
    user = await _create(admin)
    async with _client(app) as browser:
        await _login(browser, user.username.upper())
        assert (await browser.get("/api/me")).json()["user"] == user.model_dump()
        listed = (await browser.get("/api/users")).json()
        assert listed == {"items": [user.model_dump()], "next_offset": None}
        response = await browser.patch("/api/users/" + user.id, json={"enabled": False})
        assert response.status_code == 409 and response.json() == {"error": "self_disable"}
        assert (await browser.get("/api/me")).status_code == 200
        assert (await _create(browser, "second-admin")).role == "platform_admin"


@pytest.mark.parametrize(
    "change",
    [
        {"username": 1},
        {"name": []},
        {"role": "owner"},
        {"role": {}},
        {"tenant_id": 1},
        {"password": False},
        {"password": "short"},
        {"password": "a" * 129},
        {"name": ""},
        {"role": "tenant_user"},
        {"tenant_id": "not-a-uuid"},
        {"actor_id": "bootstrap"},
        {"enabled": True},
        {"revision": 1},
        {"username": "bad username"},
    ],
)
async def test_account_create_rejects_invalid_types_membership_and_extra_authority(
    api: tuple[httpx.AsyncClient, Starlette], change: dict[str, object]
) -> None:
    """Closed bodies never coerce role, membership, status, or audit attribution."""
    admin, _ = api
    response = await admin.post("/api/users", json=_body() | change)
    assert response.status_code == 400 and response.json() == {"error": "validation"}
    assert (await admin.get("/api/users")).json()["items"] == []


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"name": 1},
        {"enabled": 1},
        {"enabled": "false"},
        {"role": "tenant_user"},
        {"tenant_id": None},
        {"username": "replacement"},
        {"password": PASSWORD},
        {"revision": 99},
    ],
)
async def test_account_update_rejects_invalid_and_immutable_fields(
    api: tuple[httpx.AsyncClient, Starlette], change: dict[str, object]
) -> None:
    """Even platform administrators cannot transfer membership or rewrite account authority."""
    admin, _ = api
    user = await _create(admin)
    response = await admin.patch("/api/users/" + user.id, json=change)
    assert response.status_code == 400
    assert (await admin.get("/api/users")).json()["items"] == [user.model_dump()]


async def test_account_duplicate_missing_and_display_update(api: tuple[httpx.AsyncClient, Starlette]) -> None:
    """Normalized duplicate names conflict; ordinary edits succeed without changing role or membership."""
    admin, _ = api
    user = await _create(admin)
    duplicate = await admin.post("/api/users", json=_body(user.username.upper()))
    assert duplicate.status_code == 409 and duplicate.json() == {"error": "conflict"}
    changed = await admin.patch("/api/users/" + user.id, json={"name": "Renamed"})
    assert changed.status_code == 200
    assert changed.json()["name"] == "Renamed" and changed.json()["revision"] > user.revision
    assert changed.json()["role"] == user.role and changed.json()["tenant_id"] is None
    for method, path, body in [
        ("PATCH", "/api/users/missing", {"name": "N"}),
        ("POST", "/api/users/missing/password", {"password": PASSWORD}),
    ]:
        assert (await admin.request(method, path, json=body)).status_code == 404


@pytest.mark.parametrize("offset", ["-100", "1", "101", "1100", "abc", "1.0", ""])
async def test_account_pagination_rejects_invalid_offsets(
    api: tuple[httpx.AsyncClient, Starlette], offset: str
) -> None:
    """Pages are fixed, nonnegative, bounded integer offsets rather than unbounded scans."""
    admin, app = api
    with patch.object(app.state.admin.users, "list_users", AsyncMock()) as query:
        response = await admin.get("/api/users", params={"offset": offset})
    assert response.status_code == 400
    query.assert_not_awaited()


async def test_account_pagination_next_last_and_empty(api: tuple[httpx.AsyncClient, Starlette]) -> None:
    """Real retained rows produce a next link only when more public accounts exist."""
    admin, app = api
    first = await _create(admin)
    database = app.state.store._db
    async with database.transaction():
        for index in range(100):
            await database.execute(
                "INSERT INTO saas_users SELECT ?,?,name,role,tenant_id,enabled,revision,?,password_hash "
                "FROM saas_users WHERE id=?",
                (f"00000000-0000-0000-0000-{index:012d}", f"page-{index}", first.created_at + index + 1, first.id),
            )
    page = (await admin.get("/api/users")).json()
    assert len(page["items"]) == 100 and page["next_offset"] == 100
    last = (await admin.get("/api/users?offset=100")).json()
    assert len(last["items"]) == 1 and last["next_offset"] is None
    assert (await admin.get("/api/users?offset=1000")).json() == {"items": [], "next_offset": None}


async def test_account_password_change_requires_current_and_revokes_every_session(
    api: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """A successful change advances persistence revision and rejects all previously issued cookies."""
    admin, app = api
    user = await _create(admin)
    async with _client(app) as first, _client(app) as second:
        await _login(first, user.username)
        await _login(second, user.username)
        old_cookie = first.cookies.get(COOKIE_NAME)
        wrong = await first.post(
            "/api/password", json={"current_password": "incorrect-password", "new_password": NEW_PASSWORD}
        )
        assert wrong.status_code == 400 and (await second.get("/api/me")).status_code == 200
        changed = await first.post("/api/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
        assert changed.status_code == 200 and changed.json() == {"changed": True}
        assert "Max-Age=0" in changed.headers["set-cookie"]
        assert (await second.get("/api/session")).status_code == 401
        assert (await first.get("/api/me", headers={"cookie": f"{COOKIE_NAME}={old_cookie}"})).status_code == 401
        assert (
            await first.post("/api/login", json={"username": user.username, "password": PASSWORD})
        ).status_code == 401
        await _login(first, user.username, NEW_PASSWORD)
        assert (await first.get("/api/me")).json()["user"]["revision"] > user.revision


@pytest.mark.parametrize("mutation", ["reset", "disable", "tenant"])
async def test_account_revision_invalidates_cookies_without_intermediate_request(
    api: tuple[httpx.AsyncClient, Starlette], mutation: str
) -> None:
    """Reset or disable/re-enable cannot revive a cookie that was never presented while disabled."""
    admin, app = api
    tenant = (await admin.post("/api/tenants", json={"name": "Member tenant"})).json()["id"]
    body = _body() | {"role": "tenant_user", "tenant_id": tenant}
    user = UserAccount.model_validate((await admin.post("/api/users", json=body)).json())
    async with _client(app) as browser:
        await _login(browser, user.username)
        cookie = browser.cookies.get(COOKIE_NAME)
        assert cookie is not None and app.state.admin.sessions.identity(cookie) is not None
        if mutation == "reset":
            assert (
                await admin.post(f"/api/users/{user.id}/password", json={"password": NEW_PASSWORD})
            ).status_code == 200
        else:
            path = f"/api/users/{user.id}" if mutation == "disable" else f"/api/tenants/{tenant}"
            for enabled in (False, True):
                assert (await admin.patch(path, json={"enabled": enabled})).status_code == 200
        assert app.state.admin.sessions.identity(cookie) is None
        assert (await browser.get("/api/me")).status_code == 401
        await _login(browser, user.username, NEW_PASSWORD if mutation == "reset" else PASSWORD)
        assert (await browser.get("/api/me")).status_code == 200


async def test_account_login_unknown_disabled_and_wrong_are_generic(api: tuple[httpx.AsyncClient, Starlette]) -> None:
    """Neither account existence nor enabled state is revealed by credential failures."""
    admin, app = api
    user = await _create(admin)
    async with _client(app) as browser:
        for username, password in [("unknown", PASSWORD), (user.username, "incorrect-password")]:
            response = await browser.post("/api/login", json={"username": username, "password": password})
            assert response.status_code == 401 and response.json() == {"error": "unauthorized"}
        assert (await admin.patch("/api/users/" + user.id, json={"enabled": False})).status_code == 200
        response = await browser.post("/api/login", json={"username": user.username, "password": PASSWORD})
        assert response.status_code == 401 and response.json() == {"error": "unauthorized"}


@pytest.mark.parametrize(
    "body,status",
    [
        ({"username": 1, "password": PASSWORD}, 401),
        ({"username": "a" * 129, "password": PASSWORD}, 401),
        ({"username": "user", "password": []}, 401),
        ({"username": "user", "password": "a" * 129}, 401),
        ({"username": "user"}, 400),
        ({"username": "user", "password": PASSWORD, "role": "platform_admin"}, 400),
        ({"token": 1}, 401),
        ({"token": "a" * 257}, 401),
    ],
)
async def test_account_login_closed_input(
    api: tuple[httpx.AsyncClient, Starlette], body: dict[str, object], status: int
) -> None:
    """Malformed credentials and extra client authority are rejected before account authentication."""
    _, app = api
    async with _client(app) as browser:
        assert (await browser.post("/api/login", json=body)).status_code == status


async def test_account_csrf_and_password_input_boundaries(api: tuple[httpx.AsyncClient, Starlette]) -> None:
    """Account writes retain session-bound CSRF and strict, non-coercing password objects."""
    admin, app = api
    user = await _create(admin)
    async with _client(app) as browser:
        await _login(browser, user.username)
        for body in (
            {"current_password": 1, "new_password": NEW_PASSWORD},
            {"current_password": PASSWORD, "new_password": None},
            {"current_password": PASSWORD, "new_password": NEW_PASSWORD, "role": "platform_admin"},
        ):
            assert (await browser.post("/api/password", json=body)).status_code == 400
        writes: list[tuple[str, dict[str, object]]] = [
            ("/api/users", _body("other")),
            (f"/api/users/{user.id}/password", {"password": NEW_PASSWORD}),
            ("/api/password", {"current_password": PASSWORD, "new_password": NEW_PASSWORD}),
        ]
        for path, payload in writes:
            assert (await browser.post(path, json=payload, headers={"x-csrf-token": "forged"})).status_code == 403
    assert (await admin.post(f"/api/users/{user.id}/password", json={"password": 1})).status_code == 400


async def test_account_hash_saturation_returns_safe_429(api: tuple[httpx.AsyncClient, Starlette]) -> None:
    """The shared password work admission boundary translates saturation into a static HTTP category."""
    _, app = api
    hasher = app.state.admin.users._passwords
    with patch.object(hasher, "_admitted", HASH_ADMISSION):
        async with _client(app) as browser:
            response = await browser.post("/api/login", json={"username": "unknown", "password": PASSWORD})
    assert response.status_code == 429 and response.json() == {"error": "capacity"}


def test_account_identity_accessor_rejects_unverified_and_invalid_state() -> None:
    """Internal callers cannot mistake arbitrary request state for verified account authority."""
    request = Request({"type": "http"})
    with pytest.raises(RuntimeError, match="not been authenticated"):
        account(request)
    request.state.authenticated, request.state.account = True, {"role": "platform_admin"}
    with pytest.raises(RuntimeError, match="Invalid browser identity"):
        account(request)


async def test_account_expiring_cookie_is_rechecked_before_platform_dispatch(
    api: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """A cookie expiring between middleware and authorization cannot reach the platform handler."""
    admin, app = api
    with patch.object(app.state.admin.sessions, "verify", side_effect=["csrf", None]):
        response = await admin.get("/api/users")
    assert response.status_code == 401 and response.json() == {"error": "unauthorized"}


@pytest.mark.parametrize("state", ["deleted", "disabled", "tenant"])
async def test_account_revalidation_rejects_disappearing_or_disabled_authority(
    api: tuple[httpx.AsyncClient, Starlette], state: str
) -> None:
    """Asynchronous account and tenant reads must each fail closed if authority changes during a request."""
    admin, app = api
    tenant = await app.state.store.create_tenant("Membership")
    created = await admin.post("/api/users", json=_body() | {"role": "tenant_user", "tenant_id": tenant.id})
    user = UserAccount.model_validate(created.json())
    async with _client(app) as browser:
        await _login(browser, user.username)
        if state == "tenant":
            tenant.enabled = False
            with patch.object(app.state.store, "get_tenant", AsyncMock(return_value=tenant)):
                response = await browser.get("/api/me")
        else:
            current = None if state == "deleted" else user.model_copy(update={"enabled": False})
            with patch.object(app.state.admin.users, "get_user", AsyncMock(return_value=current)):
                response = await browser.get("/api/me")
        assert response.status_code == 401 and response.json() == {"error": "unauthorized"}
        assert (await browser.get("/api/me")).status_code == 401


async def test_account_session_and_login_admission_remain_bounded(api: tuple[httpx.AsyncClient, Starlette]) -> None:
    """Account login cannot exceed either the existing session cap or global login-attempt admission."""
    admin, app = api
    user = await _create(admin)
    async with _client(app) as browser:
        with patch.object(app.state.admin.sessions, "_limit", 1):
            response = await browser.post("/api/login", json={"username": user.username, "password": PASSWORD})
        assert response.status_code == 401
        with patch.object(app.state.admin.logins, "limit", 0):
            response = await browser.post("/api/login", json={"username": user.username, "password": PASSWORD})
        assert response.status_code == 429 and response.json() == {"error": "rate_limit"}
