"""Isolated hosted administration, upload validation, HTTP admission and lifecycle tests."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from gryphon.saas import create_app
from gryphon.saas_config import SaaSConfig
from gryphon.saas_http import HTTPBoundary

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.requests import Request
    from starlette.types import Message

    from gryphon.config import GryphonConfig

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Offline", "version": "1"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {},
}
CHANNEL = {"name": "Compute", "spec_ids": [], "sandbox_mode": "restricted", "allowed_imports": []}


@pytest.fixture
def hosted_config(tmp_path: Path) -> SaaSConfig:
    """Use independent random credentials, memory SQLite and temporary hosted execution stores."""
    return SaaSConfig(
        admin_token=SecretStr(secrets.token_urlsafe(48)),
        database_url=SecretStr("sqlite:///:memory:"),
        public_origin="https://testserver",
        state_dir=tmp_path / "hosted",
        max_spec_bytes=2048,
    )


@pytest.fixture
async def app(hosted_config: SaaSConfig, gryphon_config: GryphonConfig) -> AsyncIterator[Starlette]:
    """Initialize and close the actual application without touching operator state."""
    application = create_app(hosted_config, gryphon_config)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def http(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    """Exercise HTTPS cookie handling through the complete ASGI middleware stack."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://testserver") as client:
        yield client


@pytest.fixture
async def admin(http: httpx.AsyncClient, hosted_config: SaaSConfig) -> httpx.AsyncClient:
    """Log in through the real endpoint and retain session-bound CSRF state."""
    response = await http.post("/api/login", json={"token": hosted_config.admin_token.get_secret_value()})
    assert response.status_code == 200
    http.headers["x-csrf-token"] = response.json()["csrf_token"]
    return http


@pytest.fixture
async def tenant(admin: httpx.AsyncClient) -> str:
    """Create server-generated tenant identity using authenticated HTTP."""
    response = await admin.post("/api/tenants", json={"name": "Tenant"})
    assert response.status_code == 201
    return str(response.json()["id"])


async def test_saas_session_csrf_logout_and_role_separation(http: httpx.AsyncClient, hosted_config: SaaSConfig) -> None:
    """Bootstrap bearer alone grants no admin access; cookies and session CSRF are both necessary."""
    assert (await http.get("/api/session")).status_code == 401
    token = hosted_config.admin_token.get_secret_value()
    assert (await http.get("/api/tenants", headers={"Authorization": f"Bearer {token}"})).status_code == 401
    login = await http.post("/api/login", json={"token": token})
    cookie = login.headers["set-cookie"].lower()
    assert all(value in cookie for value in ["httponly", "secure", "samesite=strict", "path=/api"])
    csrf = login.json()["csrf_token"]
    assert (await http.get("/api/session")).json() == {"csrf_token": csrf}
    for headers in ({}, {"x-csrf-token": "wrong"}):
        assert (await http.post("/api/tenants", json={"name": "Denied"}, headers=headers)).status_code == 403
    assert (await http.post("/api/logout", headers={"x-csrf-token": csrf})).status_code == 200
    assert (await http.get("/api/session")).status_code == 401


@pytest.mark.parametrize("token", ["wrong", 42, "x" * 257])
async def test_saas_login_invalid_credentials_denied(http: httpx.AsyncClient, token: object) -> None:
    """Malformed credentials never issue a browser session."""
    response = await http.post("/api/login", json={"token": token})
    assert response.status_code == 401 and "set-cookie" not in response.headers


@pytest.mark.parametrize("path", ["/", "/health", "/api/settings", "/static/admin.js", "/missing"])
async def test_saas_responses_have_browser_security_headers(admin: httpx.AsyncClient, path: str) -> None:
    """HTML, API, static files and ordinary errors cannot cache authority or permit framing."""
    response = await admin.get(path)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "max-age=" in response.headers["strict-transport-security"]


@pytest.mark.parametrize("name", ["", " ", 1, "x" * 129, "bad\nname", "bad\x00name"])
async def test_saas_tenant_invalid_name_refused(admin: httpx.AsyncClient, name: object) -> None:
    """Display names reject control characters and coercion before persistence."""
    assert (await admin.post("/api/tenants", json={"name": name})).status_code == 400
    assert (await admin.get("/api/tenants")).json() == {"items": []}


@pytest.mark.parametrize(
    "content",
    ["{", "[]", '{"name":NaN}', '{"name":"ok","owner":"forged"}', '{"name":' + "[" * 70 + "0" + "]" * 70 + "}"],
)
async def test_saas_json_body_shape_and_depth_refused(admin: httpx.AsyncClient, content: str) -> None:
    """Malformed, nonfinite, deep and authority-bearing bodies receive safe validation errors."""
    response = await admin.post("/api/tenants", content=content, headers={"content-type": "application/json"})
    assert response.status_code == 400 and response.json() == {"error": "validation"}


async def test_saas_non_json_mutation_refused(admin: httpx.AsyncClient) -> None:
    """Form posts cannot bypass JSON and CSRF request contracts."""
    assert (await admin.post("/api/tenants", data={"name": "Form"})).status_code == 400


@pytest.mark.parametrize(
    "content",
    [
        json.dumps(SPEC),
        "openapi: 3.0.3\ninfo: {title: Offline, version: '1'}\nservers: [{url: 'https://api.example.com'}]\npaths: {}",
    ],
)
async def test_saas_upload_json_and_yaml_round_trip(admin: httpx.AsyncClient, tenant: str, content: str) -> None:
    """Both upload formats produce immutable tenant-scoped canonical documents."""
    path = f"/api/tenants/{tenant}/specs"
    uploaded = await admin.post(path, json={"name": "weather", "content": content})
    assert uploaded.status_code == 201 and "document" not in uploaded.json()
    listing = (await admin.get(path)).json()["items"]
    assert len(listing) == 1 and "document" not in listing[0]
    document = (await admin.get(path + "/" + uploaded.json()["id"])).json()["document"]
    assert document["openapi"] == "3.0.3"


@pytest.mark.parametrize(
    "content",
    [
        "[",
        "[]",
        "x: !!binary aGVsbG8=",
        "x: 2026-01-01",
        "x: .nan",
        "x: &loop [*loop]",
        "x: " + "[" * 70 + "0" + "]" * 70,
        " " * 2049,
        json.dumps({**SPEC, "x": {"$ref": "https://evil.example/schema"}}),
        json.dumps({**SPEC, "x": {"$ref": "file:///etc/passwd"}}),
        json.dumps({**SPEC, "x": {"$ref": 7}}),
        "{}",
    ],
)
async def test_saas_upload_unsafe_documents_refused(admin: httpx.AsyncClient, tenant: str, content: str) -> None:
    """No external references, YAML host types, cycles, malformed or oversized trees are accepted."""
    path = f"/api/tenants/{tenant}/specs"
    response = await admin.post(path, json={"name": "safe", "content": content})
    assert response.status_code == 400 and response.json() == {"error": "validation"}
    assert (await admin.get(path)).json()["items"] == []


@pytest.mark.parametrize("name", ["../escape", "a/b", "class", "bad\nname"])
async def test_saas_upload_unsafe_module_names_refused(admin: httpx.AsyncClient, tenant: str, name: str) -> None:
    """Uploaded display names cannot turn into unsafe catalog module paths."""
    response = await admin.post(f"/api/tenants/{tenant}/specs", json={"name": name, "content": json.dumps(SPEC)})
    assert response.status_code == 400


async def test_saas_foreign_specs_and_colliding_bindings_refused(admin: httpx.AsyncClient, tenant: str) -> None:
    """Foreign tenant IDs and normalized duplicate bindings cannot be published."""
    foreign = (await admin.post("/api/tenants", json={"name": "Other"})).json()["id"]
    ids = []
    for name in ["weather-api", "weather_api"]:
        uploaded = await admin.post(f"/api/tenants/{tenant}/specs", json={"name": name, "content": json.dumps(SPEC)})
        assert uploaded.status_code == 201
        ids.append(uploaded.json()["id"])
    response = await admin.get(f"/api/tenants/{foreign}/specs/{ids[0]}")
    assert response.status_code == 404
    for owner, selection, status in [(foreign, ids[:1], 404), (tenant, ids, 400), (tenant, ids[:1] * 2, 400)]:
        response = await admin.post(f"/api/tenants/{owner}/channels", json={**CHANNEL, "spec_ids": selection})
        assert response.status_code == status
    assert (await admin.get(f"/api/tenants/{tenant}/channels")).json()["items"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"sandbox_mode": "docker"},
        {"sandbox_mode": "host"},
        {"allowed_imports": ["math"]},
        {"allowed_imports": ["os"]},
        {"allowed_imports": "math"},
        {"allowed_imports": ["math", "math"]},
        {"spec_ids": ["x"] * 101},
        {"spec_ids": [42]},
    ],
)
async def test_saas_channel_profiles_fail_closed(
    admin: httpx.AsyncClient, tenant: str, changes: dict[str, object]
) -> None:
    """Restricted imports stay empty and Docker requires explicit operator enablement."""
    response = await admin.post(f"/api/tenants/{tenant}/channels", json={**CHANNEL, **changes})
    assert response.status_code == 400


async def test_saas_tenant_channel_revisions_and_key_lifecycle(
    admin: httpx.AsyncClient, tenant: str, app: Starlette
) -> None:
    """Disable, enable, rotate and revoke preserve configuration but change authentication authority."""
    prefix = f"/api/tenants/{tenant}"
    channel = (await admin.post(prefix + "/channels", json=CHANNEL)).json()
    path = prefix + "/channels/" + channel["id"]
    assert not channel["key_active"]
    first = (await admin.post(path + "/rotate")).json()["token"]
    second = (await admin.post(path + "/rotate")).json()["token"]
    assert await app.state.store.lookup_key(first) is None
    assert await app.state.store.lookup_key(second) is not None
    for enabled in [False, True]:
        assert (await admin.patch(prefix, json={"enabled": enabled})).json()["enabled"] is enabled
        assert (await app.state.store.lookup_key(second) is not None) is enabled
    update = await admin.patch(path, json={**CHANNEL, "enabled": False})
    assert update.json()["revision"] > channel["revision"]
    assert await app.state.store.lookup_key(second) is None
    assert (await admin.patch(path, json={**CHANNEL, "enabled": True})).status_code == 200
    assert (await admin.post(path + "/revoke")).json() == {"revoked": True}
    assert await app.state.store.lookup_key(second) is None
    assert (await admin.get(prefix + "/usage")).json() == {"items": []}
    audit = (await admin.get("/api/audit")).json()["items"]
    assert {"key_rotated", "key_revoked", "tenant_disabled", "tenant_enabled"} <= {item["event"] for item in audit}
    assert all(set(item) == {"id", "tenant_id", "channel_id", "event", "created_at", "actor"} for item in audit)
    assert all(
        item["actor"]
        == {"id": "bootstrap", "username": None, "name": None, "kind": "bootstrap", "display_source": "snapshot"}
        for item in audit
    )


@pytest.mark.parametrize(
    "headers,status,error",
    [
        ({"host": "evil.example"}, 400, "invalid_host"),
        ({"origin": "https://evil.example"}, 403, "invalid_origin"),
        ({"sec-fetch-site": "cross-site"}, 403, "invalid_origin"),
        ({"content-encoding": "gzip"}, 415, "invalid_encoding"),
        ({"content-length": "-1"}, 400, "validation"),
        ({"content-length": "1048577"}, 400, "validation"),
    ],
)
async def test_saas_http_untrusted_headers_refused(
    http: httpx.AsyncClient, headers: dict[str, str], status: int, error: str
) -> None:
    """Host, browser origin, compression and declared request limits apply before routing."""
    response = await http.get("/health", headers=headers)
    assert response.status_code == status and response.json() == {"error": error}
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("header", ["host", "origin", "authorization", "content-length"])
async def test_saas_duplicate_authority_headers_refused(http: httpx.AsyncClient, header: str) -> None:
    """Ambiguous duplicate authority fields are never normalized into a trusted identity."""
    response = await http.get("/health", headers=[(header, "one"), (header, "two")])
    assert response.status_code == 400 and response.json() == {"error": "invalid_headers"}


async def test_saas_login_rate_bound_is_global(http: httpx.AsyncClient, app: Starlette) -> None:
    """Forwarded identity cannot create an unbounded login-rate bucket."""
    app.state.admin.logins.limit = 1
    assert (await http.post("/api/login", json={"token": "wrong"})).status_code == 401
    response = await http.post("/api/login", json={"token": "wrong"}, headers={"x-forwarded-for": "203.0.113.1"})
    assert response.status_code == 429


async def test_saas_concurrency_and_rate_bounds_release_slots(hosted_config: SaaSConfig) -> None:
    """Slow admitted requests cannot exceed constant-memory concurrency; completed work releases slots."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def endpoint(request: Request) -> JSONResponse:
        """Hold one request until the competing admission attempt has finished."""
        entered.set()
        await release.wait()
        return JSONResponse({"ok": True})

    hosted_config.max_http_requests, hosted_config.requests_per_minute = 1, 2
    boundary = HTTPBoundary(Starlette(routes=[Route("/", endpoint)]), hosted_config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(boundary), base_url=hosted_config.public_origin
    ) as client:
        pending = asyncio.create_task(client.get("/"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert (await client.get("/")).status_code == 429
        finally:
            release.set()
            await pending
        assert boundary.active == 0
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/")).status_code == 429


@pytest.mark.parametrize("failure", ["disconnect", "chunked", "timeout"])
async def test_saas_body_production_bounds(hosted_config: SaaSConfig, failure: str) -> None:
    """Streaming bodies, disconnects and stalled upload producers cannot hold admission indefinitely."""
    boundary = HTTPBoundary(AsyncMock(), hosted_config)
    hosted_config.request_timeout_seconds = 1

    async def receive() -> Message:
        """Supply adversarial framing without allocating unbounded memory."""
        if failure == "timeout":
            await asyncio.sleep(2)
        if failure == "disconnect":
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": b"x" * 2049, "more_body": True}

    send = AsyncMock()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/tenants/id/specs",
        "scheme": "https",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
    }
    await boundary(scope, receive, send)
    assert send.call_args_list[0].args[0]["status"] == (408 if failure == "timeout" else 400)
    assert boundary.active == 0


@pytest.mark.parametrize("stage", ["initialize", "acquire_host_lease"])
async def test_saas_partial_startup_closes_store(
    hosted_config: SaaSConfig, gryphon_config: GryphonConfig, stage: str
) -> None:
    """Failures before readiness still close the store that may be partially initialized."""
    application = create_app(hosted_config, gryphon_config)
    with (
        patch.object(application.state.store, stage, AsyncMock(side_effect=RuntimeError("startup"))),
        patch.object(application.state.store, "close", AsyncMock(wraps=application.state.store.close)) as close,
    ):
        with pytest.raises(RuntimeError, match="startup"):
            async with application.router.lifespan_context(application):
                pytest.fail("A partially initialized app must not become ready")
        close.assert_awaited_once()


async def test_saas_lifespan_closes_sessions_runtimes_and_store(
    hosted_config: SaaSConfig, gryphon_config: GryphonConfig
) -> None:
    """Normal shutdown invalidates browser credentials and closes owned resources."""
    application = create_app(hosted_config, gryphon_config)
    async with application.router.lifespan_context(application):
        issued = application.state.admin.sessions.login(hosted_config.admin_token.get_secret_value())
        assert issued is not None
    assert application.state.admin.sessions.verify(issued[0]) is None
    assert application.state.runtimes._closed
    await application.state.store.close()


@pytest.mark.parametrize("enabled", [0, 1, "true", None])
async def test_saas_enabled_state_requires_boolean(admin: httpx.AsyncClient, tenant: str, enabled: object) -> None:
    """Tenant and channel mutations never coerce truthy strings or integers into authority."""
    prefix = f"/api/tenants/{tenant}"
    channel = (await admin.post(prefix + "/channels", json=CHANNEL)).json()["id"]
    assert (await admin.patch(prefix, json={"enabled": enabled})).status_code == 400
    response = await admin.patch(prefix + "/channels/" + channel, json={**CHANNEL, "enabled": enabled})
    assert response.status_code == 400


async def test_saas_upload_requires_text_content(admin: httpx.AsyncClient, tenant: str) -> None:
    """A JSON object cannot bypass the upload parser's raw byte and tree bounds."""
    response = await admin.post(f"/api/tenants/{tenant}/specs", json={"name": "weather", "content": SPEC})
    assert response.status_code == 400
