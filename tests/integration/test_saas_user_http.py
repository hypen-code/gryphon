"""Real HTTP account isolation and tenant-user FastMCP execution against temporary SQLite."""

from __future__ import annotations

import json
import secrets
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import Client
from pydantic import SecretStr
from test_saas_http import _data, _server

from gryphon.models import UserAccount
from gryphon.saas_auth import COOKIE_NAME
from gryphon.saas_config import SaaSConfig
from gryphon.saas_store import SaaSStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.applications import Starlette

    from gryphon.config import GryphonConfig

PASSWORD = "disposable-member-password-73"
RESOURCE_METHODS = (
    "set_tenant_enabled",
    "list_specs",
    "create_spec",
    "get_spec",
    "list_channels",
    "create_channel",
    "update_channel",
    "rotate_key",
    "revoke_key",
    "get_channel",
    "list_usage",
)
POLICY: dict[str, object] = {"name": "compute", "spec_ids": [], "sandbox_mode": "restricted", "allowed_imports": []}


@dataclass
class Members:
    """Keep independent verified browser sessions and their persisted server-issued identities."""

    admin: httpx.AsyncClient
    first: httpx.AsyncClient
    second: httpx.AsyncClient
    app: Starlette
    users: tuple[UserAccount, UserAccount]
    tenants: tuple[str, str]


async def _login(http: httpx.AsyncClient, username: str) -> None:
    """Exchange account credentials through the actual network boundary."""
    response = await http.post("/api/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200 and set(response.json()) == {"csrf_token"}
    http.headers["x-csrf-token"] = response.json()["csrf_token"]


async def _user(http: httpx.AsyncClient, username: str, tenant: str | None = None) -> UserAccount:
    """Persist accounts using platform administration, never inject sessions or identity state."""
    response = await http.post(
        "/api/users",
        json={
            "username": username,
            "name": username + " private name",
            "password": PASSWORD,
            "role": "platform_admin" if tenant is None else "tenant_user",
            "tenant_id": tenant,
        },
    )
    assert response.status_code == 201
    return UserAccount.model_validate(response.json())


def _config(tmp_path: Path) -> SaaSConfig:
    """Build explicit settings with only test-generated credentials and temporary local persistence."""
    return SaaSConfig(
        admin_token=SecretStr(secrets.token_urlsafe(48)),
        database_url=SecretStr("sqlite:///" + str(tmp_path / "control.db")),
        public_origin="http://127.0.0.1",
        allow_insecure_http=True,
        state_dir=tmp_path / "hosted",
    )


@pytest.fixture
async def members(gryphon_config: GryphonConfig, tmp_path: Path) -> AsyncIterator[Members]:
    """Bootstrap a persisted administrator and two independently scoped member browser sessions."""
    config = _config(tmp_path)
    async with _server(config, gryphon_config) as (origin, app), httpx.AsyncClient(base_url=origin) as admin:
        response = await admin.post("/api/login", json={"token": config.admin_token.get_secret_value()})
        assert response.status_code == 200
        admin.headers["x-csrf-token"] = response.json()["csrf_token"]
        operator = await _user(admin, "persisted-admin")
        await _login(admin, operator.username)
        first = (await admin.post("/api/tenants", json={"name": "First private tenant"})).json()["id"]
        second = (await admin.post("/api/tenants", json={"name": "Foreign private tenant"})).json()["id"]
        users = (await _user(admin, "first-member", first), await _user(admin, "foreign-member", second))
        async with httpx.AsyncClient(base_url=origin) as one, httpx.AsyncClient(base_url=origin) as two:
            await _login(one, users[0].username)
            await _login(two, users[1].username)
            yield Members(admin, one, two, app, users, (first, second))


async def _publish(http: httpx.AsyncClient, tenant: str, name: str) -> dict[str, str]:
    """Create a real immutable catalog, isolated channel and independent key as the current browser."""
    prefix = "/api/tenants/" + tenant
    document = {
        "openapi": "3.0.3",
        "info": {"title": name, "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {"/current": {"get": {"operationId": "current", "responses": {"200": {"description": "OK"}}}}},
    }
    spec = await http.post(prefix + "/specs", json={"name": name, "content": json.dumps(document)})
    assert spec.status_code == 201
    policy = POLICY | {"name": name, "spec_ids": [spec.json()["id"]]}
    channel = await http.post(prefix + "/channels", json=policy)
    assert channel.status_code == 201
    path = prefix + "/channels/" + channel.json()["id"]
    rotated = await http.post(path + "/rotate")
    assert rotated.status_code == 200
    return {
        "spec": spec.json()["id"],
        "channel": channel.json()["id"],
        "path": path,
        "token": rotated.json()["token"],
        "url": str(http.base_url).rstrip("/") + rotated.json()["endpoint"],
    }


async def test_member_real_http_own_catalog_execution_and_foreign_runtime_handles(members: Members) -> None:
    """Tenant-user-created keys execute Monty while real foreign receipts and recipes remain isolated."""
    first = await _publish(members.first, members.tenants[0], "weather")
    second = await _publish(members.second, members.tenants[1], "inventory")
    async with Client(first["url"], auth=first["token"]) as client:
        assert [item["name"] for item in (await _data(client, "list_servers"))["servers"]] == ["weather"]
        result = await _data(
            client, "execute_code", {"code": 'result = inputs["n"] * 2', "description": "Double", "inputs": {"n": 21}}
        )
        assert result["success"] and result["data"] == 42
        large = await _data(client, "execute_code", {"code": 'result = "a" * 20000', "description": "Artifact"})
        receipt = await _data(client, "get_run", {"run_id": large["run_id"]})
        artifact = receipt["result"]["artifact_id"]
    async with Client(second["url"], auth=second["token"]) as client:
        assert (await _data(client, "execute_code", {"code": "result = 7", "description": "Own compute"}))["data"] == 7
        for tool, args, category in [
            ("get_run", {"run_id": result["run_id"]}, "not_found"),
            ("cancel_run", {"run_id": result["run_id"]}, "not_found"),
            ("run_cached_code", {"cache_id": result["cache_id"]}, "cache"),
            ("read_artifact", {"artifact_id": artifact}, "cache"),
        ]:
            denied = await _data(client, tool, args)
            assert not denied["success"] and denied["error_type"] == category
    foreign_key = await members.first.post(
        first["url"], headers={"authorization": "Bearer " + second["token"]}, json={}
    )
    assert foreign_key.status_code == 401
    prefix = "/api/tenants/" + members.tenants[0]
    for suffix in ("/specs", "/specs/" + first["spec"], "/channels", "/usage"):
        assert (await members.first.get(prefix + suffix)).status_code == 200
    usage = (await members.first.get(prefix + "/usage")).json()["items"]
    assert any(item["tool"] == "execute_code" and item["calls"] == 2 for item in usage)
    updated = await members.first.patch(first["path"], json=POLICY | {"enabled": True})
    assert updated.status_code == 200
    assert (await members.first.post(first["path"] + "/revoke")).json() == {"revoked": True}
    assert (
        await members.first.post(first["url"], headers={"authorization": "Bearer " + first["token"]}, json={})
    ).status_code == 401


async def test_member_foreign_paths_denied_before_resource_store_queries(members: Members) -> None:
    """Every foreign tenant resource route fails before handler lookup, parsing, or mutation."""
    foreign = await _publish(members.second, members.tenants[1], "inventory")
    prefix = "/api/tenants/" + members.tenants[1]
    routes = [
        ("PATCH", ""),
        ("GET", "/specs"),
        ("POST", "/specs"),
        ("GET", "/specs/" + foreign["spec"]),
        ("GET", "/channels"),
        ("POST", "/channels"),
        ("PATCH", "/channels/" + foreign["channel"]),
        ("POST", "/channels/" + foreign["channel"] + "/rotate"),
        ("POST", "/channels/" + foreign["channel"] + "/revoke"),
        ("GET", "/usage"),
    ]
    with ExitStack() as stack:
        spies = [
            stack.enter_context(
                patch.object(
                    members.app.state.store, name, AsyncMock(side_effect=AssertionError("Foreign resource queried"))
                )
            )
            for name in RESOURCE_METHODS
        ]
        own_tenant = stack.enter_context(
            patch.object(members.app.state.store, "get_tenant", wraps=members.app.state.store.get_tenant)
        )
        for method, suffix in routes:
            response = await members.first.request(
                method,
                prefix + suffix,
                content=b"not-json",
                headers={"x-role": "platform_admin", "x-tenant-id": members.tenants[1]},
            )
            assert response.status_code == 404 and response.json() == {"error": "not_found"}
        for spy in spies:
            spy.assert_not_awaited()
        assert all(call.args == (members.tenants[0],) for call in own_tenant.await_args_list)
    assert (await members.first.get("/api/tenants/not-real/specs")).status_code == 404


async def test_member_foreign_handles_under_own_prefix_are_not_found(members: Members) -> None:
    """Rewriting only the tenant prefix cannot steal another tenant's spec or channel handles."""
    foreign = await _publish(members.second, members.tenants[1], "inventory")
    prefix = "/api/tenants/" + members.tenants[0]
    assert (await members.first.get(prefix + "/specs/" + foreign["spec"])).status_code == 404
    for suffix in ("/rotate", "/revoke"):
        assert (await members.first.post(prefix + "/channels/" + foreign["channel"] + suffix)).status_code == 404
    assert (
        await members.first.patch(prefix + "/channels/" + foreign["channel"], json=POLICY | {"enabled": True})
    ).status_code == 404
    assert (
        await members.first.post(prefix + "/channels", json=POLICY | {"spec_ids": [foreign["spec"]]})
    ).status_code == 404


async def test_member_platform_endpoints_forbid_claimed_roles_and_own_user_targets(members: Members) -> None:
    """Account listings, creation, changes and resets require platform authority, even for oneself."""
    with ExitStack() as stack:
        spies = [
            stack.enter_context(patch.object(members.app.state.admin.users, name, AsyncMock()))
            for name in ("list_users", "create_user", "update_user", "reset_password")
        ]
        targets = [("GET", "/api/users"), ("POST", "/api/users")]
        for user in members.users:
            targets.extend([("PATCH", "/api/users/" + user.id), ("POST", "/api/users/" + user.id + "/password")])
        for method, path in targets:
            response = await members.first.request(
                method,
                path,
                params={"role": "platform_admin", "tenant_id": members.tenants[0]},
                headers={"x-user-id": members.users[1].id, "x-role": "platform_admin"},
                json={"role": "platform_admin", "tenant_id": members.tenants[1], "enabled": True},
            )
            assert response.status_code == 403 and response.json() == {"error": "forbidden"}
        for spy in spies:
            spy.assert_not_awaited()
    assert (await members.first.post("/api/tenants", json={"name": "Escalation"})).status_code == 403
    assert (await members.first.patch("/api/tenants/" + members.tenants[0], json={"enabled": False})).status_code == 403
    assert (await members.first.patch("/api/tenants/" + members.tenants[1], json={"enabled": False})).status_code == 404


async def test_member_identity_discovery_and_audit_are_scoped_and_secret_free(members: Members) -> None:
    """Server state, not headers or query parameters, decides membership and serialized audit visibility."""
    for browser, user, tenant in zip((members.first, members.second), members.users, members.tenants, strict=True):
        me = await browser.get("/api/me", params={"role": "platform_admin"}, headers={"x-tenant-id": "forged"})
        assert me.json()["user"] == user.model_dump()
        tenants = (await browser.get("/api/tenants?role=platform_admin")).json()["items"]
        assert [item["id"] for item in tenants] == [tenant]
        audit = await browser.get("/api/audit?tenant_id=forged")
        assert audit.status_code == 200 and audit.json()["items"]
        assert all(event["tenant_id"] == tenant for event in audit.json()["items"])
        assert PASSWORD not in audit.text and "password_hash" not in audit.text and "pbkdf2" not in audit.text
        assert all(other.name not in audit.text and other.username not in audit.text for other in members.users)
    audit = (await members.admin.get("/api/audit")).json()["items"]
    account_events = [event for event in audit if "subject_id" in event]
    assert {event["subject_id"] for event in account_events} >= {user.id for user in members.users}
    for event in account_events:
        assert set(event) == {
            "id",
            "actor_id",
            "subject_id",
            "subject_name",
            "subject_username",
            "tenant_id",
            "event",
            "created_at",
            "actor",
        }
        assert event["subject_name"] is None and event["subject_username"] is None
        assert set(event["actor"]) == {"id", "username", "name", "kind", "display_source"}
        assert event["actor"]["id"] == event["actor_id"]


async def test_member_credentials_cannot_cross_bootstrap_password_or_channel_sessions(members: Members) -> None:
    """Neither a bootstrap token as password nor a channel key as cookie grants browser authority."""
    channel = await _publish(members.first, members.tenants[0], "weather")
    async with httpx.AsyncClient(base_url=members.first.base_url) as anonymous:
        for body in (
            {
                "username": members.users[0].username,
                "password": members.app.state.admin.config.admin_token.get_secret_value(),
            },
            {"username": members.users[0].username, "password": channel["token"]},
            {"token": channel["token"]},
        ):
            response = await anonymous.post("/api/login", json=body)
            assert response.status_code == 401 and response.json() == {"error": "unauthorized"}
        for path in ("/api/me", "/api/users", "/api/tenants"):
            response = await anonymous.get(
                path,
                headers={"authorization": "Bearer " + channel["token"], "cookie": f"{COOKIE_NAME}={channel['token']}"},
            )
            assert response.status_code == 401


async def test_member_password_change_preserves_role_and_invalidates_another_browser(members: Members) -> None:
    """Self-service changes remain available to tenant users without granting platform authority."""
    async with httpx.AsyncClient(base_url=members.first.base_url) as another:
        await _login(another, members.users[0].username)
        response = await members.first.post(
            "/api/password", json={"current_password": PASSWORD, "new_password": "new-disposable-password-29"}
        )
        assert response.status_code == 200 and response.json() == {"changed": True}
        assert (await another.get("/api/me")).status_code == 401
        login = await members.first.post(
            "/api/login", json={"username": members.users[0].username, "password": "new-disposable-password-29"}
        )
        assert login.status_code == 200
        me = (await members.first.get("/api/me")).json()["user"]
        assert me["role"] == "tenant_user" and me["tenant_id"] == members.tenants[0]
        assert (await members.first.get("/api/users")).status_code == 403


async def test_accounts_additive_sqlite_upgrade_preserves_legacy_tenant_and_key(
    gryphon_config: GryphonConfig, tmp_path: Path
) -> None:
    """A disposable pre-account schema upgrades in place and retains legacy channel authorization."""
    config = _config(tmp_path)
    store = SaaSStore(config.database_url.get_secret_value())
    await store.initialize()
    try:
        tenant = await store.create_tenant("Legacy retained tenant")
        channel = await store.create_channel(
            tenant.id, "Legacy compute", spec_ids=[], sandbox_mode="restricted", allowed_imports=[]
        )
        key = await store.rotate_key(tenant.id, channel.id)
        async with store._db.transaction():
            await store._db.execute("DROP TABLE saas_user_audit")
            await store._db.execute("DROP TABLE saas_users")
    finally:
        await store.close()
    async with _server(config, gryphon_config) as (origin, _), httpx.AsyncClient(base_url=origin) as admin:
        response = await admin.post("/api/login", json={"token": config.admin_token.get_secret_value()})
        admin.headers["x-csrf-token"] = response.json()["csrf_token"]
        assert (await admin.get("/api/users")).json() == {"items": [], "next_offset": None}
        assert (await admin.get("/api/tenants")).json()["items"] == [tenant.model_dump()]
        user = await _user(admin, "persisted-after-upgrade", tenant.id)
        async with Client(origin + "/mcp/" + channel.id, auth=key) as client:
            assert (await _data(client, "execute_code", {"code": "result = 42", "description": "Legacy"}))["data"] == 42
    async with _server(config, gryphon_config) as (origin, _), httpx.AsyncClient(base_url=origin) as member:
        await _login(member, user.username)
        assert (await member.get("/api/me")).json()["user"] == user.model_dump()
        assert [item["id"] for item in (await member.get("/api/tenants")).json()["items"]] == [tenant.id]
