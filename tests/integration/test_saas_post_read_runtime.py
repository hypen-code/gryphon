"""Real hosted MCP execution/replay with automatic catalog POSTs and synthetic HTTP only."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import Client
from test_saas_http import _data
from test_saas_http import hosted as hosted
from test_saas_spec_filter import _catalog_count, _publish
from test_saas_user_http import POLICY

from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.applications import Starlette

    from gryphon.config import GryphonConfig


async def _execute(channel: dict[str, str], function: str) -> dict[str, Any]:
    """Execute canonical two-argument broker calls through actual restricted Monty."""
    async with Client(channel["url"], auth=channel["token"]) as client:
        return await _data(
            client,
            "execute_code",
            {
                "code": f'result = await call_tool("{function}", {{"json_body": {{"symbol": inputs["symbol"]}}}})',
                "description": "Synthetic included POST",
                "inputs": {"symbol": "SYN &="},
            },
        )


async def _replay(channel: dict[str, str], cache_id: str) -> dict[str, Any]:
    """Replay complete structured inputs under the channel key, never caller-selected ownership."""
    async with Client(channel["url"], auth=channel["token"]) as client:
        return await _data(client, "run_cached_code", {"cache_id": cache_id, "params": {"symbol": "NEXT"}})


def _network(requests: list[httpx.Request]) -> Callable[[GryphonConfig], NetworkClient]:
    """Retain production DNS pinning with an isolated synthetic transport and no live upstream."""

    def response(request: httpx.Request) -> httpx.Response:
        """Record only synthetic DNS-pinned transport traffic."""
        requests.append(request)
        return httpx.Response(200, json={"synthetic": True})

    def network(config: GryphonConfig) -> NetworkClient:
        """Construct the real bounded client without a real DNS resolver."""
        return NetworkClient(
            config, resolver=AsyncMock(return_value=["93.184.216.34"]), transport=httpx.MockTransport(response)
        )

    return network


@pytest.mark.parametrize("change", ["filter", "unbind"])
@pytest.mark.parametrize(
    "function,media_type",
    [
        ("cse.get_company_info_summery", "application/x-www-form-urlencoded"),
        ("cse.get_company_profile", "multipart/form-data"),
        ("cse.get_company_info_video", "multipart/form-data"),
    ],
)
async def test_hosted_automatic_posts_replay_until_filter_or_binding_changes(
    hosted: tuple[httpx.AsyncClient, Starlette], function: str, media_type: str, change: str
) -> None:
    """Source filtering and exact binding removal revoke dispatch and invalidate existing recipes."""
    http, app = hosted
    prefix, spec, channel = await _publish(http, filtered=False)
    _, _, foreign = await _publish(http, filtered=True)
    assert spec["approved_post_reads"] == []
    requests: list[httpx.Request] = []
    with patch("gryphon.security.broker.NetworkClient", side_effect=_network(requests)):
        executed = await _execute(channel, function)
        assert executed["success"] and executed["data"] == {"synthetic": True}
        replay = await _replay(channel, executed["cache_id"])
        assert replay["success"] and replay["data"] == executed["data"] and len(requests) == 2
        assert all(
            request.method == "POST" and request.headers["content-type"].startswith(media_type) for request in requests
        )
        assert all(request.headers["host"] == "market.example" for request in requests)
        assert all("authorization" not in request.headers and "cookie" not in request.headers for request in requests)
        denied = await _execute(foreign, function)
        assert not denied["success"] and denied["error_type"] == "not_found" and len(requests) == 2
        assert not (await _replay(foreign, executed["cache_id"]))["success"] and len(requests) == 2
        if change == "filter":
            changed = await http.post(
                prefix + "/specs/" + spec["id"] + "/filter", json={"read_only_filter": True, "update_channels": True}
            )
            assert changed.status_code == 201 and await _catalog_count(channel) == 1
        else:
            changed = await http.patch(prefix + "/channels/" + channel["id"], json=POLICY | {"enabled": True})
            assert changed.status_code == 200
        denied = await _execute(channel, function)
        assert not denied["success"] and denied["error_type"] == "not_found" and len(requests) == 2
        assert not (await _replay(channel, executed["cache_id"]))["success"] and len(requests) == 2
    assert app.state.runtimes._base.allowed_read_only_post_operations == []


async def test_hosted_filter_changes_diagnostics_catalog_and_post_authority(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Only explicit source selection changes inclusion; the retired review endpoint has no authority."""
    http, _ = hosted
    prefix, spec, channel = await _publish(http)
    assert await _catalog_count(channel) == 1
    path = prefix + "/specs/"
    before = [(await http.get(route)).json() for route in [prefix + "/specs", prefix + "/channels", "/api/audit"]]
    for method in ["GET", "POST"]:
        assert (
            await http.request(method, path + spec["id"] + "/post-reads", json={"functions": []})
        ).status_code == 404
    assert [
        (await http.get(route)).json() for route in [prefix + "/specs", prefix + "/channels", "/api/audit"]
    ] == before
    included = await http.post(path + spec["id"] + "/filter", json={"read_only_filter": False, "update_channels": True})
    assert included.status_code == 201 and included.json()["approved_post_reads"] == []
    assert included.json()["diagnostics"]["available_operations"] == await _catalog_count(channel) == 7
    with patch("gryphon.security.broker.NetworkClient.request", AsyncMock(return_value=httpx.Response(200, json={}))):
        assert (await _execute(channel, "cse.get_company_profile"))["success"]
    filtered = await http.post(
        path + included.json()["id"] + "/filter", json={"read_only_filter": True, "update_channels": True}
    )
    assert filtered.status_code == 201
    assert filtered.json()["diagnostics"]["available_operations"] == await _catalog_count(channel) == 1
