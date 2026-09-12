"""Real hosted HTTP/MCP discovery flags are channel-owned and revision-invalidated."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastmcp import Client
from test_saas_http import _channel, _data
from test_saas_http import hosted as hosted

from gryphon.errors import ConflictError
from gryphon.runtime.context import json_bytes

if TYPE_CHECKING:
    import httpx
    from starlette.applications import Starlette

    from gryphon.saas_runtime import ChannelRuntimeManager


def _policy(channel: dict[str, str], enabled: bool) -> dict[str, Any]:
    """Retain the full channel policy while changing only the discovery presentation flag."""
    return {
        "name": "Detailed",
        "spec_ids": [channel["spec"]],
        "sandbox_mode": "restricted",
        "allowed_imports": [],
        "include_function_summaries": enabled,
    }


async def _detailed_channel(http: httpx.AsyncClient, compact: dict[str, str]) -> dict[str, str]:
    """Bind the same immutable catalog to a second independently authenticated channel."""
    created = await http.post(compact["tenant"] + "/channels", json=_policy(compact, True))
    assert created.status_code == 201 and created.json()["include_function_summaries"] is True
    path = compact["tenant"] + "/channels/" + created.json()["id"]
    rotated = await http.post(path + "/rotate")
    assert rotated.status_code == 200
    return {
        **compact,
        "id": created.json()["id"],
        "path": path,
        "token": rotated.json()["token"],
        "url": str(http.base_url).rstrip("/") + rotated.json()["endpoint"],
    }


async def _listing(channel: dict[str, str], summaries: bool) -> dict[str, Any]:
    """Read actual MCP metadata and prove no client-selected mode or endpoint tools are advertised."""
    async with Client(channel["url"], auth=channel["token"]) as client:
        tools = await client.list_tools()
        assert len(tools) == 11
        discovery = next(tool for tool in tools if tool.name == "list_servers")
        assert "include_function_summaries" not in discovery.input_schema["properties"]
        assert ("with function names and descriptions" in str(discovery.description)) is summaries
        page = await _data(client, "list_servers")
        assert len(json_bytes(page)) <= 16384
        row = page["servers"][0]
        if summaries:
            assert row["functions"] == [{"name": "current", "description": "Current value"}]
            assert page["next_function_cursor"] is None
        else:
            assert set(row) == {"name", "description", "function_count"}
            assert "next_function_cursor" not in page
        search = await _data(client, "search_functions", {"query": "current"})
        assert search["functions"][0]["function_name"] == "current"
        return page


async def test_hosted_summary_channels_isolated_and_toggle_retires_old_lifecycle(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Same-catalog channels differ only by their persisted flag and toggles revoke stale runtimes."""
    http, app = hosted
    compact = await _channel(http, "weather")
    detailed = await _detailed_channel(http, compact)
    compact_page = await _listing(compact, False)
    detailed_page = await _listing(detailed, True)
    assert compact_page["registry_fingerprint"] == detailed_page["registry_fingerprint"]
    manager: ChannelRuntimeManager = app.state.runtimes
    for enabled in [False, True]:
        old = manager._runtimes[detailed["id"]]
        response = await http.patch(detailed["path"], json={**_policy(detailed, enabled), "enabled": True})
        assert response.status_code == 200 and response.json()["include_function_summaries"] is enabled
        assert response.json()["revision"] == old.channel.revision + 1
        assert not old.verifier.active and old.closing is not None and old.closing.done()
        with pytest.raises(ConflictError):
            await manager._acquire(old.channel, [])
        assert await _listing(compact, False) == compact_page
        new_page = await _listing(detailed, enabled)
        assert new_page["registry_fingerprint"] != detailed_page["registry_fingerprint"]
        detailed_page = new_page
        assert manager._runtimes[detailed["id"]] is not old
