"""Hosted native MCP immutable snapshots, safe failures, filtering and ownership."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx
import pytest
from fastmcp import Client
from test_saas_http import _channel, _data
from test_saas_http import hosted as hosted
from test_saas_ucp_mcp import (
    CODE,
    ENDPOINT,
    ORIGIN,
    READS,
    SEQUENCE,
    NativePeer,
    arguments,
    execute,
    import_bound,
    inspect_catalog,
    native_tool,
)
from test_saas_ucp_mcp import native_peer as native_peer
from test_saas_user_http import _login, _user

if TYPE_CHECKING:
    from starlette.applications import Starlette

    from gryphon.models import SpecImport


async def test_native_ucp_refresh_adds_lookup_read_and_retains_original_json(
    hosted: tuple[httpx.AsyncClient, Starlette], native_peer: NativePeer
) -> None:
    """Standard catalog.lookup advertises get_product without any proprietary Shopify extension."""
    http, _ = hosted
    native_peer.tools = [tool for tool in native_peer.tools if tool["name"] != "get_product"]
    channel = await _channel(http, "weather")
    original, revision = await import_bound(http, channel)
    async with Client(channel["url"], auth=channel["token"]) as client:
        await inspect_catalog(client, READS - {"get_product"})
    native_peer.tools.append(native_tool("get_product"))
    path = channel["tenant"] + "/specs/" + original["id"]
    response = await http.post(path + "/refresh", json={"update_channels": True})
    assert response.status_code == 201, response.text
    updated = response.json()
    assert updated["parent_id"] == original["id"] and updated["diagnostics"]["available_operations"] == 6
    assert set(updated["mcp_bindings"]) == READS
    assert (await http.get(path)).json() == original
    bound = (await http.get(channel["tenant"] + "/channels")).json()["items"][0]
    assert bound["revision"] == revision + 1 and bound["spec_ids"] == [updated["id"]]
    async with Client(channel["url"], auth=channel["token"]) as client:
        await inspect_catalog(client, READS)
    assert native_peer.methods() == SEQUENCE * 2


async def _cancel_fixture(client: Client[Any], peer: NativePeer, *, filtered: bool) -> None:
    """Probe automatic included-POST dispatch only against the synthetic peer, never a live cart."""
    before = len(peer.seen)
    result = await _data(
        client,
        "execute_code",
        {
            "code": CODE.replace("shop.search_catalog", "shop.cancel_cart"),
            "description": "Synthetic cancel dispatch",
            "inputs": arguments(),
        },
    )
    if filtered:
        assert not result["success"] and result["error_type"] == "not_found", result
        assert len(peer.seen) == before
    else:
        assert result["success"] and result["data"] == 10, result
        assert peer.methods()[-5:] == SEQUENCE + ["tools/call"]


async def test_native_ucp_filter_is_network_free_and_preserves_all_native_bindings(
    hosted: tuple[httpx.AsyncClient, Starlette], native_peer: NativePeer
) -> None:
    """Unchecking includes a supported side-effect tool; a readOnlyHint cannot select it while checked."""
    http, _ = hosted
    native_peer.tools.append(native_tool("cancel_cart"))
    channel = await _channel(http, "weather")
    original, revision = await import_bound(http, channel)
    assert set(original["mcp_bindings"]) == READS | {"cancel_cart"}
    assert original["diagnostics"]["available_operations"] == 6
    assert original["diagnostics"]["filtered_operations"] == 1
    previous = original
    for enabled, expected in [(False, READS | {"cancel_cart"}), (True, READS)]:
        before = len(native_peer.seen)
        response = await http.post(
            channel["tenant"] + "/specs/" + previous["id"] + "/filter",
            json={"read_only_filter": enabled, "update_channels": True},
        )
        assert response.status_code == 201, response.text
        current = response.json()
        assert len(native_peer.seen) == before
        assert current["mcp_bindings"] == original["mcp_bindings"] and current["sha256"] == original["sha256"]
        assert current["source_url"] == original["source_url"] and current["resolved_endpoint"] == ENDPOINT
        assert current["diagnostics"]["available_operations"] == len(expected)
        async with Client(channel["url"], auth=channel["token"]) as client:
            await inspect_catalog(client, expected)
            assert len(native_peer.seen) == before
            await _cancel_fixture(client, native_peer, filtered=enabled)
        previous = current
    bound = (await http.get(channel["tenant"] + "/channels")).json()["items"][0]
    assert bound["revision"] == revision + 2 and bound["spec_ids"] == [previous["id"]]
    assert (await http.get(channel["tenant"] + "/specs/" + original["id"])).json() == original


@pytest.mark.parametrize("failure", ["protocol", "bad_schema", "unsupported_only"])
async def test_native_ucp_failed_discovery_keeps_snapshots_and_bindings(
    hosted: tuple[httpx.AsyncClient, Starlette], native_peer: NativePeer, failure: str
) -> None:
    """Import and refresh reject invalid protocol/schema metadata without partial publication or raw errors."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    original, _ = await import_bound(http, channel)
    prefix = channel["tenant"] + "/specs"
    before_specs = (await http.get(prefix)).json()
    before_channels = (await http.get(channel["tenant"] + "/channels")).json()
    native_peer.failure = failure
    if failure == "bad_schema":
        native_peer.tools[0]["inputSchema"] = {"type": "array"}
    elif failure == "unsupported_only":
        for tool in native_peer.tools:
            tool["inputSchema"]["allOf"] = [{}]
    for route, body in [
        (prefix, {"name": "invalid", "url": ORIGIN, "kind": "ucp"}),
        (prefix + "/" + original["id"] + "/refresh", {"update_channels": True}),
    ]:
        response = await http.post(route, json=body)
        assert response.status_code == 400 and response.json()["error"] == "ucp_discovery", response.text
        assert "private" not in response.text
    assert (await http.get(prefix)).json() == before_specs
    assert (await http.get(channel["tenant"] + "/channels")).json() == before_channels
    assert (await http.get(prefix + "/" + original["id"])).json() == original
    assert "tools/call" not in native_peer.methods()
    assert all(client._client.is_closed for client in native_peer.clients)


async def test_native_ucp_tool_error_is_safe_and_does_not_mutate_snapshot(
    hosted: tuple[httpx.AsyncClient, Starlette], native_peer: NativePeer
) -> None:
    """Native isError is an execution failure, never successful product data or exposed upstream text."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    original, _ = await import_bound(http, channel)
    before_channels = (await http.get(channel["tenant"] + "/channels")).json()
    native_peer.failure = "isError"
    async with Client(channel["url"], auth=channel["token"]) as client:
        result = await execute(client)
        assert not result["success"] and result["error_type"] == "execution", result
        assert "private-upstream-detail" not in str(result)
    assert (await http.get(channel["tenant"] + "/specs/" + original["id"])).json() == original
    assert (await http.get(channel["tenant"] + "/channels")).json() == before_channels
    assert native_peer.methods() == SEQUENCE * 2 + ["tools/call"]


@pytest.mark.parametrize(
    ("invalid", "error_type"),
    [("missing_agent", "upstream"), ("unknown_nested", "validation"), ("missing_wrapper", "validation")],
)
async def test_native_ucp_requires_caller_metadata_and_closed_wrapped_arguments(
    hosted: tuple[httpx.AsyncClient, Starlette], native_peer: NativePeer, invalid: str, error_type: str
) -> None:
    """Neither Gryphon nor the native provider generates missing caller identity or relaxes nested input."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    await import_bound(http, channel)
    supplied = arguments()
    code = CODE
    if invalid == "missing_agent":
        del supplied["meta"]
    elif invalid == "unknown_nested":
        supplied["catalog"]["destination"] = "https://untrusted.example/mcp"
    else:
        code = CODE.replace('{"json_body": inputs}', "inputs")
    before = len(native_peer.seen)
    async with Client(channel["url"], auth=channel["token"]) as client:
        result = await _data(client, "execute_code", {"code": code, "description": "Invalid input", "inputs": supplied})
        assert not result["success"] and result["error_type"] == error_type, result
        if invalid == "missing_agent":
            assert result["diagnostic"] == {
                "kind": "upstream",
                "phase": "invoke",
                "upstream_code": "invalid_profile_url",
            }
        else:
            assert "diagnostic" not in result
    assert len(native_peer.seen) == before
    assert native_peer.profile_dns == []


@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], ["93.184.216.34", "10.0.0.1"]])
async def test_native_ucp_profile_dns_denied_before_protocol_dispatch(
    hosted: tuple[httpx.AsyncClient, Starlette], native_peer: NativePeer, addresses: list[str]
) -> None:
    """A syntactically public identity must independently pass every DNS answer, without fetching it."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    await import_bound(http, channel)
    native_peer.profile_addresses = addresses
    before = len(native_peer.seen)
    async with Client(channel["url"], auth=channel["token"]) as client:
        result = await execute(client)
    assert not result["success"] and result["error_type"] == "upstream", result
    assert result["diagnostic"] == {"kind": "upstream", "phase": "invoke", "upstream_code": "invalid_profile_url"}
    assert native_peer.profile_dns == [("caller.example", 443)]
    assert len(native_peer.seen) == before
    assert native_peer.clients[-1]._client.is_closed


@pytest.mark.parametrize("field", ["mcp_bindings", "source_transport", "resolved_endpoint"])
@pytest.mark.parametrize("source", ["file", "url"])
async def test_native_ucp_ordinary_spec_post_cannot_inject_transport_authority(
    hosted: tuple[httpx.AsyncClient, Starlette], native_peer: NativePeer, field: str, source: str
) -> None:
    """Only host discovery may write trusted transport/binding fields; normal POST bodies are closed."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    value: object = {"injected": {"endpoint": ENDPOINT}} if field == "mcp_bindings" else ENDPOINT
    body: dict[str, object] = {"name": "injected", "url": ORIGIN, "kind": "ucp", field: value}
    if source == "file":
        saved = (await http.get(channel["tenant"] + "/specs/" + channel["spec"])).json()
        body = {"name": "injected", "content": json.dumps(saved["document"]), field: value}
    response = await http.post(channel["tenant"] + "/specs", json=body)
    assert response.status_code == 400
    assert native_peer.seen == []


async def test_native_ucp_tenant_ownership_and_foreign_channel_key_denied(
    hosted: tuple[httpx.AsyncClient, Starlette], native_peer: NativePeer
) -> None:
    """Tenant membership and channel keys are independently verified before native network access."""
    http, _ = hosted
    first, second = await _channel(http, "weather"), await _channel(http, "foreign")
    original, _ = await import_bound(http, first)
    await _user(http, "native-member", first["tenant"].rsplit("/", 1)[-1])
    before = len(native_peer.seen)
    async with httpx.AsyncClient(base_url=http.base_url) as member:
        await _login(member, "native-member")
        prefix = second["tenant"] + "/specs"
        for route, body in [
            (prefix, {"name": "foreign", "url": ORIGIN, "kind": "ucp"}),
            (prefix + "/" + original["id"] + "/refresh", {}),
            (prefix + "/" + original["id"] + "/filter", {"read_only_filter": False}),
        ]:
            response = await member.post(route, json=body)
            assert response.status_code == 404 and response.json() == {"error": "not_found"}
        response = await member.post(first["url"], headers={"Authorization": "Bearer " + second["token"]}, json={})
        assert response.status_code == 401
    # A platform session also cannot cross-join a valid spec ID to the wrong tenant.
    response = await http.post(second["tenant"] + "/specs/" + original["id"] + "/refresh", json={})
    assert response.status_code == 404
    assert len(native_peer.seen) == before


async def test_native_ucp_rechecks_tenant_after_discovery_before_publication(
    hosted: tuple[httpx.AsyncClient, Starlette], native_peer: NativePeer
) -> None:
    """An import that loses membership authority while awaiting real metadata cannot publish its result."""
    http, app = hosted
    channel = await _channel(http, "weather")
    original, _ = await import_bound(http, channel)
    tenant_id = channel["tenant"].rsplit("/", 1)[-1]
    await _user(http, "racing-member", tenant_id)
    original_load = app.state.admin.importer.load

    async def load_then_disable(*args: Any, **kwargs: Any) -> SpecImport:
        """Complete the real native import, then revoke the tenant before route authorization recheck."""
        imported: SpecImport = await original_load(*args, **kwargs)
        await app.state.store.set_tenant_enabled(tenant_id, False)
        return imported

    before = (await http.get(channel["tenant"] + "/specs")).json()
    async with httpx.AsyncClient(base_url=http.base_url) as member:
        await _login(member, "racing-member")
        with patch.object(app.state.admin.importer, "load", load_then_disable):
            response = await member.post(channel["tenant"] + "/specs/" + original["id"] + "/refresh", json={})
        assert response.status_code in {401, 403}
    assert (await http.get(channel["tenant"] + "/specs")).json() == before
    assert "tools/call" not in native_peer.methods()
