"""Real loopback modern MCP conformance for independently authenticated hosted channels."""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import uvicorn
from fastmcp import Client
from pydantic import SecretStr

from gryphon.errors import CapacityError, ConflictError, SecurityViolationError
from gryphon.saas import create_app
from gryphon.saas_config import SaaSConfig
from gryphon.saas_gateway import _successful, _tool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.applications import Starlette

    from gryphon.config import GryphonConfig


@asynccontextmanager
async def _server(config: SaaSConfig, base: GryphonConfig) -> AsyncIterator[tuple[str, Starlette]]:
    """Serve the real hosted app with an explicitly allowed loopback origin on a reserved port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    config = config.model_copy(update={"public_origin": origin, "allow_insecure_http": True})
    application = create_app(config, base)
    server = uvicorn.Server(
        uvicorn.Config(application, log_level="error", lifespan="on", access_log=False, proxy_headers=False)
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("Hosted server exited before readiness")
                await asyncio.sleep(0.01)
        yield origin, application
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


@pytest.fixture
async def hosted(gryphon_config: GryphonConfig, tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Starlette]]:
    """Use only disposable stores, fresh random credentials and real loopback administration."""
    config = SaaSConfig(
        admin_token=SecretStr(secrets.token_urlsafe(48)),
        database_url=SecretStr("sqlite:///:memory:"),
        public_origin="http://127.0.0.1",
        allow_insecure_http=True,
        state_dir=tmp_path / "hosted",
    )
    async with _server(config, gryphon_config) as (origin, application), httpx.AsyncClient(base_url=origin) as http:
        response = await http.post("/api/login", json={"token": config.admin_token.get_secret_value()})
        assert response.status_code == 200
        http.headers["x-csrf-token"] = response.json()["csrf_token"]
        yield http, application


async def _channel(http: httpx.AsyncClient, name: str) -> dict[str, str]:
    """Publish one immutable offline catalog under a new tenant and independently rotate its key."""
    tenant = await http.post("/api/tenants", json={"name": name})
    assert tenant.status_code == 201
    prefix = "/api/tenants/" + tenant.json()["id"]
    document = {
        "openapi": "3.0.3",
        "info": {"title": name, "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/current": {
                "get": {
                    "operationId": "current",
                    "summary": "Current value",
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }
    uploaded = await http.post(prefix + "/specs", json={"name": name, "content": json.dumps(document)})
    assert uploaded.status_code == 201
    data = {"name": name, "spec_ids": [uploaded.json()["id"]], "sandbox_mode": "restricted", "allowed_imports": []}
    channel = await http.post(prefix + "/channels", json=data)
    assert channel.status_code == 201
    path = prefix + "/channels/" + channel.json()["id"]
    rotated = await http.post(path + "/rotate")
    assert rotated.status_code == 200
    return {
        "tenant": prefix,
        "path": path,
        "id": channel.json()["id"],
        "spec": uploaded.json()["id"],
        "token": rotated.json()["token"],
        "url": str(http.base_url).rstrip("/") + rotated.json()["endpoint"],
    }


async def _data(client: Client[Any], tool: str, arguments: dict[str, object] | None = None) -> dict[str, Any]:
    """Require native structured MCP data rather than parsing text-only tool results."""
    response = await client.call_tool(tool, arguments)
    assert not response.is_error and isinstance(response.structured_content, dict)
    return response.structured_content


async def _workflow(client: Client[Any]) -> dict[str, Any]:
    """Run actual Monty code, replay exact source and retain portable owner-scoped handles."""
    assert client.session.protocol_version == "2026-07-28"
    assert client.server_capabilities is not None and client.server_capabilities.tasks is None
    assert not any("tasks" in name.lower() for name in (client.server_capabilities.extensions or {}))
    tools = await client.list_tools()
    assert len(tools) == 11 and all(tool.output_schema for tool in tools)
    executed = await _data(
        client, "execute_code", {"code": 'result = inputs["n"] * 2', "description": "Double", "inputs": {"n": 21}}
    )
    assert executed["success"] and executed["data"] == 42
    replay = await _data(client, "run_cached_code", {"cache_id": executed["cache_id"], "params": {"n": 4}})
    assert replay["success"] and replay["data"] == 8
    assert (await _data(client, "get_run", {"run_id": executed["run_id"]}))["status"] == "succeeded"
    denied = await _data(client, "execute_code", {"code": "import os\nresult = 1", "description": "Denied import"})
    assert not denied["success"] and denied["error_type"] == "security"
    large = await _data(client, "execute_code", {"code": 'result = "a" * 20000', "description": "Large output"})
    receipt = await _data(client, "get_run", {"run_id": large["run_id"]})
    executed["artifact_id"] = receipt["result"]["artifact_id"]
    assert (await _data(client, "read_artifact", {"artifact_id": executed["artifact_id"]}))["text"]
    return executed


async def test_saas_real_http_catalogs_execution_foreign_handles_and_metrics(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Real modern clients see only their bound catalogs, recipes, receipts and artifacts."""
    http, _ = hosted
    first, second = await _channel(http, "weather"), await _channel(http, "inventory")
    async with Client(first["url"], auth=first["token"]) as client:
        catalog = await _data(client, "list_servers")
        assert [item["name"] for item in catalog["servers"]] == ["weather"]
        details = await _data(
            client, "get_functions", {"functions": [{"server_name": "weather", "function_name": "current"}]}
        )
        assert details["functions"][0]["invocation"]["capability"] == "weather.current"
        handles = await _workflow(client)
    async with Client(second["url"], auth=second["token"]) as client:
        catalog = await _data(client, "list_servers")
        assert [item["name"] for item in catalog["servers"]] == ["inventory"]
        for tool, argument, handle, category in [
            ("get_run", "run_id", "run_id", "not_found"),
            ("cancel_run", "run_id", "run_id", "not_found"),
            ("run_cached_code", "cache_id", "cache_id", "cache"),
            ("read_artifact", "artifact_id", "artifact_id", "cache"),
        ]:
            denied = await _data(client, tool, {argument: handles[handle]})
            assert not denied["success"] and denied["error_type"] == category
        assert (await _data(client, "list_recipes"))["recipes"] == []
    usage = (await http.get(first["tenant"] + "/usage")).json()["items"]
    counts = {(item["tool"], item["status"]): item["calls"] for item in usage}
    assert counts["execute_code", "success"] == 2 and counts["execute_code", "error"] == 1
    assert counts["run_cached_code", "success"] == 1
    assert all(
        set(item) == {"channel_id", "tool", "status", "calls", "total_ms"} and item["total_ms"] >= 0 for item in usage
    )
    audit = (await http.get("/api/audit")).json()["items"]
    assert {"tenant_created", "spec_created", "channel_created", "key_rotated"} <= {item["event"] for item in audit}


async def test_saas_real_http_rotation_disable_and_reenable_new_revision(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Already-loaded authority is retired; revisions can reconnect without losing durable receipts."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    async with Client(channel["url"], auth=channel["token"]) as client:
        executed = await _data(client, "execute_code", {"code": "result = 7", "description": "Durable"})
    new_key = (await http.post(channel["path"] + "/rotate")).json()["token"]
    assert (
        await http.post(channel["url"], headers={"Authorization": "Bearer " + channel["token"]}, json={})
    ).status_code == 401
    async with Client(channel["url"], auth=new_key) as client:
        assert (await _data(client, "get_run", {"run_id": executed["run_id"]}))["result"]["data"] == 7
    for enabled in [False, True]:
        response = await http.patch(channel["tenant"], json={"enabled": enabled})
        assert response.status_code == 200
        if not enabled:
            assert (
                await http.post(channel["url"], headers={"Authorization": "Bearer " + new_key}, json={})
            ).status_code == 401
    async with Client(channel["url"], auth=new_key) as client:
        assert await client.list_tools()
    policy = {"name": "weather", "spec_ids": [channel["spec"]], "sandbox_mode": "restricted", "allowed_imports": []}
    for enabled in [False, True]:
        assert (await http.patch(channel["path"], json={**policy, "enabled": enabled})).status_code == 200
        if not enabled:
            assert (
                await http.post(channel["url"], headers={"Authorization": "Bearer " + new_key}, json={})
            ).status_code == 401
    async with Client(channel["url"], auth=new_key) as client:
        assert await client.list_tools()
    assert (await http.post(channel["path"] + "/revoke")).status_code == 200
    assert (await http.post(channel["url"], headers={"Authorization": "Bearer " + new_key}, json={})).status_code == 401


async def test_saas_real_http_channel_and_admin_credentials_are_not_interchangeable(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Neither tenant selection, bearer prefix tricks nor a different endpoint can confer authority."""
    http, app = hosted
    first, second = await _channel(http, "weather"), await _channel(http, "inventory")
    for credential in [
        "",
        "Bearer wrong",
        "Basic " + first["token"],
        "Bearer " + second["token"],
        "Bearer " + app.state.admin.config.admin_token.get_secret_value(),
    ]:
        response = await http.post(first["url"], headers={"Authorization": credential}, json={})
        assert response.status_code == 401 and response.json() == {"error": "unauthorized"}
    assert (
        await http.post("/mcp/not-a-uuid", headers={"Authorization": "Bearer " + first["token"]})
    ).status_code == 401
    async with httpx.AsyncClient(base_url=http.base_url) as anonymous:
        assert (
            await anonymous.get("/api/settings", headers={"Authorization": "Bearer " + first["token"]})
        ).status_code == 401
        assert (await anonymous.post("/api/login", json={"token": first["token"]})).status_code == 401


@pytest.mark.parametrize(
    "error,status,category",
    [
        (CapacityError, 429, "capacity"),
        (ConflictError, 409, "channel_unavailable"),
        (SecurityViolationError, 409, "channel_unavailable"),
    ],
)
async def test_saas_gateway_runtime_admission_errors_are_safe(
    hosted: tuple[httpx.AsyncClient, Starlette], error: type[Exception], status: int, category: str
) -> None:
    """Expected capacity and stale-revision failures expose static HTTP categories only."""
    http, app = hosted
    channel = await _channel(http, "weather")
    with patch.object(app.state.runtimes, "_acquire", AsyncMock(side_effect=error("private-detail"))):
        response = await http.post(channel["url"], headers={"Authorization": "Bearer " + channel["token"]}, json={})
    assert response.status_code == status and response.json() == {"error": category}


async def test_saas_gateway_rechecks_key_after_runtime_admission(hosted: tuple[httpx.AsyncClient, Starlette]) -> None:
    """A concurrently revoked key cannot dispatch after asynchronous initialization."""
    http, app = hosted
    channel = await _channel(http, "weather")
    identity = await app.state.store.lookup_key(channel["token"])
    with patch.object(app.state.store, "lookup_key", AsyncMock(side_effect=[identity, None])):
        response = await http.post(channel["url"], headers={"Authorization": "Bearer " + channel["token"]}, json={})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "body,expected",
    [
        (b"{", None),
        (b"[]", None),
        (b'{"method":"other"}', None),
        (b'{"method":"tools/call","params":[]}', None),
        (b'{"method":"tools/call","params":{"name":"unknown"}}', None),
        (b'{"method":"tools/call","params":{"name":"execute_code"}}', "execute_code"),
    ],
)
def test_saas_telemetry_only_recognizes_allowlisted_tools(body: bytes, expected: str | None) -> None:
    """Arbitrary request data cannot become a persistent metric dimension."""
    assert _tool(body) == expected


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (500, b"{}", False),
        (200, b"{", False),
        (200, b"[]", False),
        (200, b'{"error":{}}', False),
        (200, b'{"result":[]}', False),
        (200, b'{"result":{"isError":true}}', False),
        (200, b'{"result":{"structuredContent":{"success":false}}}', False),
        (200, b'{"result":{"structuredContent":{"success":true}}}', True),
        (200, b'{"result":{"structuredContent":[]}}', True),
    ],
)
def test_saas_telemetry_execution_errors_are_not_http_success(status: int, body: bytes, expected: bool) -> None:
    """A transport 200 is insufficient when JSON-RPC, MCP or structured execution reports failure."""
    assert _successful(status, bytearray(body)) is expected


async def test_saas_restart_retains_hashed_key_catalog_and_receipts(
    gryphon_config: GryphonConfig, tmp_path: Path
) -> None:
    """Reopening a disposable SQLite deployment preserves channel keys, configuration and owned receipts."""
    config = SaaSConfig(
        admin_token=SecretStr(secrets.token_urlsafe(48)),
        database_url=SecretStr("sqlite:///" + str(tmp_path / "control.db")),
        public_origin="http://127.0.0.1",
        allow_insecure_http=True,
        state_dir=tmp_path / "hosted",
    )
    async with _server(config, gryphon_config) as (origin, _), httpx.AsyncClient(base_url=origin) as http:
        login = await http.post("/api/login", json={"token": config.admin_token.get_secret_value()})
        http.headers["x-csrf-token"] = login.json()["csrf_token"]
        channel = await _channel(http, "weather")
        async with Client(channel["url"], auth=channel["token"]) as client:
            executed = await _data(client, "execute_code", {"code": "result = 42", "description": "Persistent"})
    async with (
        _server(config, gryphon_config) as (origin, _),
        Client(origin + "/mcp/" + channel["id"], auth=channel["token"]) as client,
    ):
        assert len((await _data(client, "list_servers"))["servers"]) == 1
        receipt = await _data(client, "get_run", {"run_id": executed["run_id"]})
        assert receipt["status"] == "succeeded" and receipt["result"]["data"] == 42
        replay = await _data(client, "run_cached_code", {"cache_id": executed["cache_id"]})
        assert replay["success"] and replay["data"] == 42


@pytest.mark.parametrize("imports,status", [(["math"], 200), (["os"], 400)])
async def test_saas_docker_import_selection_never_bypasses_allowlist(
    hosted: tuple[httpx.AsyncClient, Starlette],
    imports: list[str],
    status: int,
) -> None:
    """Explicit operator Docker enablement allows only reviewed imports without starting Docker."""
    http, app = hosted
    app.state.admin.config.docker_enabled = True
    channel = await _channel(http, "weather")
    policy = {"name": "offline", "spec_ids": [], "sandbox_mode": "docker", "allowed_imports": imports, "enabled": True}
    response = await http.patch(channel["path"], json=policy)
    assert response.status_code == status
    assert not app.state.runtimes._runtimes


@pytest.mark.parametrize("stage", ["create_tenant", "list_tenants"])
async def test_saas_unexpected_admin_failure_is_sanitized(
    hosted: tuple[httpx.AsyncClient, Starlette], stage: str
) -> None:
    """Unexpected backend failures return no diagnostics and do not corrupt later requests."""
    http, app = hosted
    with patch.object(app.state.store, stage, AsyncMock(side_effect=RuntimeError("private-detail"))):
        response = await http.request("POST" if stage == "create_tenant" else "GET", "/api/tenants", json={"name": "T"})
    assert response.status_code == 500 and response.json() == {"error": "internal"}
    assert response.headers["cache-control"] == "no-store"
    assert (await http.get("/health")).status_code == 200
