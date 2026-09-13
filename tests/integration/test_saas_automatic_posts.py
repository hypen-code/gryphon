"""Bound hosted catalog POSTs execute without the retired manual permission workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
from fastmcp import Client
from test_saas_http import _data
from test_saas_http import hosted as hosted
from test_saas_spec_filter import _publish

if TYPE_CHECKING:
    from starlette.applications import Starlette

_POSTS = [
    "get_market_status",
    "get_safe_market_data",
    "get_company_info_summery",
    "get_company_profile",
    "get_company_info_video",
]
_CODE = (
    "result = []\n"
    'for name in inputs["functions"]:\n'
    "    args = {}\n"
    '    if name not in ["get_market_status", "get_safe_market_data"]:\n'
    '        args = {"json_body": {"symbol": inputs["symbol"]}}\n'
    '    result.append(await call_tool("cse." + name, args))'
)


async def test_automatic_catalog_posts_execute_and_replay_without_grants(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Automatic means declared POST authority, not an assertion of side-effect-free semantics."""
    http, app = hosted
    prefix, spec, channel = await _publish(http, filtered=False)
    assert spec["approved_post_reads"] == []
    assert app.state.admin.base.allowed_read_only_post_operations == []
    network = AsyncMock(return_value=httpx.Response(200, json={"synthetic": True}))
    async with Client(channel["url"], auth=channel["token"]) as client:
        with patch("gryphon.security.broker.NetworkClient.request", network):
            first = await _data(
                client,
                "execute_code",
                {"code": _CODE, "description": "Bound POSTs", "inputs": {"functions": _POSTS, "symbol": "SYN"}},
            )
            assert first["success"] and first["data"] == [{"synthetic": True}] * len(_POSTS)
            replay = await _data(
                client,
                "run_cached_code",
                {"cache_id": first["cache_id"], "params": {"functions": _POSTS, "symbol": "SYN2"}},
            )
            assert replay["success"] and replay["data"] == first["data"]
    assert network.await_count == len(_POSTS) * 2
    for method in ("GET", "POST"):
        response = await http.request(method, prefix + "/specs/" + spec["id"] + "/post-reads", json={"functions": []})
        assert response.status_code == 404


async def test_automatic_posts_do_not_enable_other_mutating_methods_or_unbound_calls(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Automatic POST authority stays bound to the registered method and selected channel catalog."""
    http, _ = hosted
    _, _, channel = await _publish(http, filtered=False)
    async with Client(channel["url"], auth=channel["token"]) as client:
        with patch("gryphon.security.broker.NetworkClient.request", AsyncMock()) as network:
            result = await _data(
                client,
                "execute_code",
                {"code": 'result = await call_tool("cse.get_company_data_by_put", {})', "description": "Denied PUT"},
            )
            assert not result["success"] and result["error_type"] == "security"
            result = await _data(
                client,
                "execute_code",
                {"code": 'result = await call_tool("unbound.operation", {})', "description": "Unbound"},
            )
            assert not result["success"]
        network.assert_not_awaited()


async def test_read_only_filter_still_excludes_unattested_post_metadata(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Checking the HTTP-method filter remains a way to omit POST capabilities entirely."""
    http, _ = hosted
    _, spec, channel = await _publish(http, filtered=True)
    assert spec["diagnostics"]["available_operations"] == 1
    async with Client(channel["url"], auth=channel["token"]) as client:
        listing = await _data(client, "list_servers")
        assert listing["servers"][0]["function_count"] == 1
