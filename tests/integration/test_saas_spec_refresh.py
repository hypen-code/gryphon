"""Real hosted discovery, execution and replay through immutable URL-import refreshes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
from fastmcp import Client
from test_saas_http import _channel, _data
from test_saas_http import hosted as hosted

if TYPE_CHECKING:
    from starlette.applications import Starlette


def _spec(operation: str) -> dict[str, Any]:
    """Describe one synthetic GET capability without any upstream dependency."""
    return {
        "openapi": "3.0.3",
        "info": {"title": "Remote", "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {"/current": {"get": {"operationId": operation, "responses": {"200": {"description": "OK"}}}}},
    }


async def _remote_channel(http: httpx.AsyncClient) -> tuple[dict[str, str], dict[str, Any]]:
    """Publish and bind an isolated remote catalog through the real hosted API."""
    channel = await _channel(http, "weather")
    network = AsyncMock(return_value=httpx.Response(200, json=_spec("current")))
    with patch("gryphon.saas_spec_import.NetworkClient.request", network):
        response = await http.post(
            channel["tenant"] + "/specs",
            json={
                "name": "remote",
                "url": "https://example.com/openapi.json",
                "kind": "openapi",
            },
        )
    assert response.status_code == 201
    spec = response.json()
    assert spec["source_type"] == "openapi_url" and spec["diagnostics"]["available_operations"] == 1
    policy = {
        "name": "weather",
        "spec_ids": [channel["spec"], spec["id"]],
        "sandbox_mode": "restricted",
        "allowed_imports": [],
        "enabled": True,
    }
    assert (await http.patch(channel["path"], json=policy)).status_code == 200
    return channel, spec


async def test_url_import_refresh_updates_bound_catalog_and_invalidates_replay(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Refresh advances the registry fingerprint and blocks recipes compiled against old metadata."""
    http, _ = hosted
    channel, spec = await _remote_channel(http)
    async with Client(channel["url"], auth=channel["token"]) as client:
        listing = await _data(client, "list_servers")
        assert {item["name"] for item in listing["servers"]} == {"weather", "remote"}
        with patch(
            "gryphon.security.broker.NetworkClient.request", AsyncMock(return_value=httpx.Response(200, json={"n": 7}))
        ):
            executed = await _data(
                client,
                "execute_code",
                {"code": 'result = await call_tool("remote.current", {})', "description": "Read current"},
            )
            replay = await _data(client, "run_cached_code", {"cache_id": executed["cache_id"]})
        assert executed["data"] == replay["data"] == {"n": 7}
    path = channel["tenant"] + "/specs/" + spec["id"]
    network = AsyncMock(return_value=httpx.Response(200, json=_spec("latest")))
    with patch("gryphon.saas_spec_import.NetworkClient.request", network):
        refreshed = await http.post(path + "/refresh", json={"update_channels": True})
    assert refreshed.status_code == 201 and refreshed.json()["parent_id"] == spec["id"]
    assert (await http.get(path)).json()["document"] == _spec("current")
    current = (await http.get(channel["tenant"] + "/channels")).json()["items"][0]
    assert current["spec_ids"] == [channel["spec"], refreshed.json()["id"]]
    async with Client(channel["url"], auth=channel["token"]) as client:
        found = await _data(
            client, "get_functions", {"functions": [{"server_name": "remote", "function_name": "latest"}]}
        )
        assert found["functions"][0]["invocation"]["capability"] == "remote.latest"
        stale = await _data(client, "run_cached_code", {"cache_id": executed["cache_id"]})
        assert not stale["success"] and stale["error_type"] == "conflict"
    with patch("gryphon.saas_spec_import.NetworkClient.request", network):
        unchanged = await http.post(channel["tenant"] + "/specs/" + refreshed.json()["id"] + "/refresh", json={})
    assert unchanged.status_code == 200 and unchanged.json()["id"] == refreshed.json()["id"]


async def test_file_update_keeps_upload_mode_and_requires_explicit_binding_replacement(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Replacement file bytes preserve provenance and require opt-in channel rebinding."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    path = channel["tenant"] + "/specs/" + channel["spec"]
    response = await http.post(path + "/refresh", json={"content": json.dumps(_spec("latest"))})
    assert response.status_code == 201 and response.json()["source_type"] == "file"
    assert response.json()["source_url"] is None
    bound = (await http.get(channel["tenant"] + "/channels")).json()["items"][0]
    assert bound["spec_ids"] == [channel["spec"]]
    assert (await http.post(path + "/refresh", json={"url": "https://example.com/other"})).status_code == 400
    assert (await http.post(path + "/refresh", json={"content": "{}", "update_channels": "true"})).status_code == 400


async def test_refresh_bad_source_and_csrf_fail_without_altering_active_catalog(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """CSRF and invalid document failures leave stored snapshots and bindings untouched."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    path = channel["tenant"] + "/specs/" + channel["spec"] + "/refresh"
    before = (await http.get(channel["tenant"] + "/channels")).json()
    assert (await http.post(path, json={"content": "{}"}, headers={"x-csrf-token": "wrong"})).status_code == 403
    assert (await http.post(path, json={"content": "bad", "update_channels": True})).status_code == 400
    assert (await http.get(channel["tenant"] + "/channels")).json() == before
    assert len((await http.get(channel["tenant"] + "/specs")).json()["items"]) == 1
