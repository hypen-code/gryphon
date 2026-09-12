"""Hosted synthetic CSE-shaped POST permits and wire contracts, not upstream semantic attestations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import Client
from test_saas_http import _data
from test_saas_user_http import POLICY, Members
from test_saas_user_http import members as members

from gryphon.models import ReadOnlyPostOperation
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "cse_read_only_posts.yaml"
_ROUTES = ("/marketStatus", "/companyInfoSummery", "/companyProfile", "/companyInfoVideo")


def _permits(**updates: str) -> list[ReadOnlyPostOperation]:
    """Supply exact operator-owned permits independent of every uploaded hint and operation name."""
    return [
        ReadOnlyPostOperation.model_validate(
            {"server_name": "cse_api", "base_url": "https://market.example/api", "path": route, **updates}
        )
        for route in _ROUTES
    ]


def _configure(members: Members, permits: list[ReadOnlyPostOperation]) -> None:
    """Configure both operator snapshots before upload and lazy channel runtime creation."""
    members.app.state.admin.base.allowed_read_only_post_operations = permits
    members.app.state.runtimes._base.allowed_read_only_post_operations = permits


async def _publish(members: Members, available: int) -> dict[str, str]:
    """Compile the real fixture under its normalized display name and issue an isolated channel key."""
    prefix = f"/api/tenants/{members.tenants[0]}"
    response = await members.first.post(
        prefix + "/specs", json={"name": "CSE API", "content": _FIXTURE.read_text(encoding="utf-8")}
    )
    assert response.status_code == 201
    assert response.json()["diagnostics"] == {
        "total_operations": 7,
        "available_operations": available,
        "filtered_operations": 7 - available,
        "unsupported_operations": 0,
    }
    channel = await members.first.post(prefix + "/channels", json=POLICY | {"spec_ids": [response.json()["id"]]})
    assert channel.status_code == 201
    key = await members.first.post(prefix + "/channels/" + channel.json()["id"] + "/rotate")
    assert key.status_code == 200
    return {
        "id": channel.json()["id"],
        "url": str(members.first.base_url).rstrip("/") + key.json()["endpoint"],
        "token": key.json()["token"],
    }


@pytest.mark.parametrize(
    "mismatch", [None, {"server_name": "other"}, {"base_url": "https://other.example/api"}, {"path": "/other"}]
)
async def test_hosted_cse_default_or_inexact_permits_only_expose_get(
    members: Members, mismatch: dict[str, str] | None
) -> None:
    """Uploaded read-only labels and inexact operator permits cannot expose POST capabilities."""
    _configure(members, [] if mismatch is None else _permits(**mismatch))
    channel = await _publish(members, 1)
    with patch("gryphon.security.broker.NetworkClient.request") as network:
        async with Client(channel["url"], auth=channel["token"]) as client:
            listing = await _data(client, "list_servers")
            assert [(item["name"], item["function_count"]) for item in listing["servers"]] == [("cse_api", 1)]
            denied = await _data(
                client,
                "execute_code",
                {"code": 'result = await call_tool("cse_api.get_market_status", {})', "description": "Denied POST"},
            )
            assert not denied["success"]
    network.assert_not_called()


async def _exercise(client: Client[Any], function: str, arguments: dict[str, object]) -> None:
    """Inspect and execute the actual manifest capability, then replay with complete structured inputs."""
    listing = await _data(client, "list_servers")
    assert [(item["name"], item["function_count"]) for item in listing["servers"]] == [("cse_api", 5)]
    inspection = await _data(
        client, "get_functions", {"functions": [{"server_name": "cse_api", "function_name": function}]}
    )
    assert inspection["functions"][0]["invocation"]["capability"] == "cse_api." + function
    code = f'result = await call_tool("cse_api.{function}", inputs)'
    executed = await _data(client, "execute_code", {"code": code, "inputs": arguments, "description": "Fixture POST"})
    assert executed["success"] and executed["data"] == {"synthetic": True}
    replay = await _data(client, "run_cached_code", {"cache_id": executed["cache_id"], "params": arguments})
    assert replay["success"] and replay["data"] == executed["data"]


def _assert_wire(request: httpx.Request, route: str) -> None:
    """Check public-origin pinning and scalar form bytes without making claims about a live API."""
    assert request.method == "POST" and request.url.path == "/api" + route
    assert request.url.host == "93.184.216.34" and request.headers["host"] == "market.example"
    assert request.extensions["sni_hostname"] == "market.example"
    assert "authorization" not in request.headers and "cookie" not in request.headers
    if route in {"/marketStatus", "/companyInfoSummery"}:
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert request.content == (b"" if route == "/marketStatus" else b"symbol=SYN+%26%2B%3D%2F%C3%A9")
    else:
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        assert b'Content-Disposition: form-data; name="symbol"\r\n\r\nSYN &+=/\xc3\xa9\r\n' in request.content
        assert b"filename=" not in request.content


@pytest.mark.parametrize(
    ("route", "function"),
    [
        ("/marketStatus", "get_market_status"),
        ("/companyInfoSummery", "get_company_info_summery"),
        ("/companyProfile", "get_company_profile"),
        ("/companyInfoVideo", "get_company_info_video"),
    ],
)
async def test_hosted_cse_exact_permits_execute_and_replay_pinned_forms(
    members: Members, route: str, function: str
) -> None:
    """Operator permits survive upload parsing and channel deep copies through real Monty and broker dispatch."""
    permits = _permits()
    _configure(members, permits)
    channel = await _publish(members, 5)
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        """Return controlled public data and capture only synthetic transport requests."""
        requests.append(request)
        return httpx.Response(200, json={"synthetic": True})

    def network(config: GryphonConfig) -> NetworkClient:
        """Inject DNS and transport while retaining the production NetworkClient pinning path."""
        return NetworkClient(
            config, resolver=AsyncMock(return_value=["93.184.216.34"]), transport=httpx.MockTransport(transport)
        )

    arguments: dict[str, object] = {} if route == "/marketStatus" else {"json_body": {"symbol": "SYN &+=/é"}}
    with patch("gryphon.security.broker.NetworkClient", side_effect=network):
        async with Client(channel["url"], auth=channel["token"]) as client:
            await _exercise(client, function, arguments)
            broker = members.app.state.runtimes._runtimes[channel["id"]].broker
            assert broker is not None and broker._config.allowed_read_only_post_operations == permits
            assert not broker._config.allow_writes and broker._config.allowed_write_operations == []
            endpoint = broker._registry.get_endpoint("cse_api", function)
            assert endpoint.read_only_post and endpoint.path == route
    assert len(requests) == 2
    for request in requests:
        _assert_wire(request, route)
