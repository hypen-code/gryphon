"""Specification catalog visibility toggles are immutable and never authorize hosted writes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client
from test_saas_http import _channel, _data
from test_saas_http import hosted as hosted
from test_saas_spec_refresh import _remote_channel
from test_saas_spec_security import _bound, _snapshot
from test_saas_ucp_http import _import_and_bind
from test_saas_ucp_http import ucp_network as ucp_network
from test_saas_user_http import members as members

if TYPE_CHECKING:
    import httpx
    from starlette.applications import Starlette
    from test_saas_user_http import Members

    from gryphon.models import SaaSSpec, SpecImport
    from gryphon.security.network import NetworkClient

_CONTENT = (Path(__file__).parents[1] / "fixtures" / "cse_read_only_posts.yaml").read_text()


async def _publish(http: httpx.AsyncClient, *, filtered: bool = True) -> tuple[str, dict[str, Any], dict[str, str]]:
    """Bind a synthetic complete specification without enabling operator write permissions."""
    tenant = (await http.post("/api/tenants", json={"name": "Filter"})).json()["id"]
    prefix = f"/api/tenants/{tenant}"
    response = await http.post(
        prefix + "/specs", json={"name": "cse", "content": _CONTENT, "read_only_filter": filtered}
    )
    assert response.status_code == 201
    spec = response.json()
    response = await http.post(
        prefix + "/channels",
        json={
            "name": "Filtered",
            "spec_ids": [spec["id"]],
            "sandbox_mode": "restricted",
            "allowed_imports": [],
        },
    )
    assert response.status_code == 201
    channel = response.json()
    key = (await http.post(prefix + "/channels/" + channel["id"] + "/rotate")).json()
    return (
        prefix,
        spec,
        {"id": channel["id"], "url": str(http.base_url).rstrip("/") + key["endpoint"], "token": key["token"]},
    )


async def _catalog_count(channel: dict[str, str]) -> int:
    """Use real MCP discovery rather than trusting the import's persisted diagnostic count."""
    async with Client(channel["url"], auth=channel["token"]) as client:
        return int((await _data(client, "list_servers"))["servers"][0]["function_count"])


async def test_unfiltered_import_exposes_all_supported_operations_but_denies_writes(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Unticking the filter includes POST/PUT metadata without granting network execution."""
    http, app = hosted
    app.state.runtimes._base.allow_writes = True
    app.state.runtimes._base.allowed_write_operations = ["cse.get_company_profile"]
    _, spec, channel = await _publish(http, filtered=False)
    assert spec["read_only_filter"] is False
    assert spec["diagnostics"]["available_operations"] == 7 and spec["diagnostics"]["filtered_operations"] == 0
    assert await _catalog_count(channel) == 7
    with patch("gryphon.security.broker.NetworkClient.request", AsyncMock()) as network:
        async with Client(channel["url"], auth=channel["token"]) as client:
            found = await _data(client, "search_functions", {"query": "get_company_profile"})
            assert found["functions"][0]["function_name"] == "get_company_profile"
            result = await _data(
                client,
                "execute_code",
                {
                    "code": 'result = await call_tool("cse.get_company_profile", {"json_body": {"symbol": "SYN"}})',
                    "description": "Unapproved POST",
                },
            )
            assert not result["success"] and result["error_type"] == "security"
    network.assert_not_awaited()


async def test_saved_filter_toggle_uses_existing_document_and_revises_exact_bindings(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """No reupload/refetch is necessary, and old snapshots retain their original filtering."""
    http, _ = hosted
    prefix, old, channel = await _publish(http)
    assert await _catalog_count(channel) == 1
    with patch("gryphon.saas_spec_import.NetworkClient") as network:
        response = await http.post(
            prefix + "/specs/" + old["id"] + "/filter",
            json={
                "read_only_filter": False,
                "update_channels": True,
            },
        )
    assert response.status_code == 201
    new = response.json()
    assert new["parent_id"] == old["id"] and new["read_only_filter"] is False
    assert (await http.get(prefix + "/specs/" + old["id"])).json()["read_only_filter"] is True
    assert await _catalog_count(channel) == 7
    network.assert_not_called()
    response = await http.post(prefix + "/specs/" + new["id"] + "/filter", json={"read_only_filter": False})
    assert response.status_code == 200 and response.json()["id"] == new["id"]
    restored = await http.post(
        prefix + "/specs/" + new["id"] + "/filter", json={"read_only_filter": True, "update_channels": True}
    )
    assert restored.status_code == 201 and await _catalog_count(channel) == 1


async def test_refresh_retains_or_explicitly_changes_filter_and_optout_bindings(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Refresh defaults to the saved filter, while a filter-only successor can leave bindings pinned."""
    http, _ = hosted
    prefix, old, channel = await _publish(http, filtered=False)
    path = prefix + "/specs/" + old["id"]
    unchanged = await http.post(path + "/refresh", json={"content": _CONTENT})
    assert unchanged.status_code == 200 and unchanged.json()["read_only_filter"] is False
    changed = await http.post(path + "/refresh", json={"content": _CONTENT, "read_only_filter": True})
    assert changed.status_code == 201 and changed.json()["read_only_filter"] is True
    assert await _catalog_count(channel) == 7
    stale = await http.post(path + "/filter", json={"read_only_filter": True})
    assert stale.status_code == 409


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
async def test_filter_requires_strict_boolean_without_mutating_snapshots(
    hosted: tuple[httpx.AsyncClient, Starlette],
    value: object,
) -> None:
    """Coercible values cannot accidentally broaden catalog selection."""
    http, _ = hosted
    prefix, old, _ = await _publish(http)
    path = prefix + "/specs/" + old["id"]
    for suffix, payload in [
        ("/filter", {"read_only_filter": value}),
        ("/refresh", {"content": _CONTENT, "read_only_filter": value}),
    ]:
        assert (await http.post(path + suffix, json=payload)).status_code == 400
    assert (
        await http.post(prefix + "/specs", json={"name": "bad", "content": _CONTENT, "read_only_filter": value})
    ).status_code == 400
    assert len((await http.get(prefix + "/specs")).json()["items"]) == 1


async def test_remote_filter_preserves_provenance_without_fetch_and_get_only_changes_version(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """A visibility preference is versioned even when every original operation is GET."""
    http, _ = hosted
    channel, old = await _remote_channel(http)
    path = channel["tenant"] + "/specs/" + old["id"]
    with patch("gryphon.saas_spec_import.NetworkClient") as network:
        response = await http.post(path + "/filter", json={"read_only_filter": False})
    assert response.status_code == 201
    new = response.json()
    assert new["id"] != old["id"] and new["parent_id"] == old["id"]
    assert new["source_type"] == old["source_type"] == "openapi_url" and new["source_url"] == old["source_url"]
    assert new["sha256"] == old["sha256"] and new["diagnostics"] == old["diagnostics"]
    assert new["read_only_filter"] is False
    assert (await http.get(path)).json()["read_only_filter"] is True
    network.assert_not_called()


async def test_filter_endpoint_tenant_csrf_and_disabled_authority(members: Members) -> None:
    """Untrusted identifiers and disabled accounts cannot even begin saved-document recompilation."""
    old_id = await _bound(members)
    before = await _snapshot(members)
    own, foreign = (f"/api/tenants/{tenant}" for tenant in members.tenants)
    with patch.object(members.app.state.admin.importer, "refilter", AsyncMock()) as importer:
        response = await members.second.post(own + f"/specs/{old_id}/filter", json={"read_only_filter": False})
        assert response.status_code == 404
        response = await members.second.post(foreign + f"/specs/{old_id}/filter", json={"read_only_filter": False})
        assert response.status_code == 404
        response = await members.first.post(
            own + f"/specs/{old_id}/filter", json={"read_only_filter": False}, headers={"x-csrf-token": "wrong"}
        )
        assert response.status_code == 403
        assert await _snapshot(members) == before
        assert (await members.admin.patch(own, json={"enabled": False})).status_code == 200
        for client, status in [(members.first, 401), (members.admin, 409)]:
            response = await client.post(own + f"/specs/{old_id}/filter", json={"read_only_filter": False})
            assert response.status_code == status
    importer.assert_not_awaited()


async def test_filter_rechecks_session_after_recompilation(members: Members) -> None:
    """A session revoked during owned compilation cannot publish a successor or bindings."""
    old_id = await _bound(members)
    before = await _snapshot(members)
    importer = members.app.state.admin.importer
    original = importer.refilter

    async def revoke(previous: SaaSSpec, filtered: bool) -> SpecImport:
        """Complete real offline compilation and revoke the submitting browser before publication."""
        result: SpecImport = await original(previous, filtered)
        assert (await members.first.post("/api/logout")).status_code == 200
        return result

    with patch.object(importer, "refilter", side_effect=revoke):
        response = await members.first.post(
            f"/api/tenants/{members.tenants[0]}/specs/{old_id}/filter",
            json={"read_only_filter": False, "update_channels": True},
        )
    assert response.status_code == 401 and await _snapshot(members) == before


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
async def test_channel_summary_http_requires_strict_boolean(
    hosted: tuple[httpx.AsyncClient, Starlette],
    value: object,
) -> None:
    """Neither create nor update coerces untrusted JSON into a discovery preference."""
    http, _ = hosted
    prefix, spec, channel = await _publish(http)
    before = (await http.get(prefix + "/channels")).json()
    payload = {
        "name": "Discovery",
        "spec_ids": [spec["id"]],
        "sandbox_mode": "restricted",
        "allowed_imports": [],
        "include_function_summaries": value,
    }
    assert (await http.post(prefix + "/channels", json=payload)).status_code == 400
    assert (
        await http.patch(prefix + "/channels/" + channel["id"], json=payload | {"enabled": True})
    ).status_code == 400
    assert (await http.get(prefix + "/channels")).json() == before


async def test_saved_ucp_filter_preserves_adapter_limits_warnings_and_source_without_network(
    hosted: tuple[httpx.AsyncClient, Starlette],
    ucp_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    """Changing the visibility flag cannot restore UCP routes excluded by protocol adaptation."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    old, _ = await _import_and_bind(http, channel)
    requests = len(ucp_network[1])
    path = channel["tenant"] + "/specs/"
    response = await http.post(path + old["id"] + "/filter", json={"read_only_filter": False})
    assert response.status_code == 201
    new = response.json()
    assert new["source_type"] == "ucp_url" and new["source_url"] == old["source_url"]
    assert new["diagnostics"]["available_operations"] == old["diagnostics"]["available_operations"] == 1
    assert set(old["warnings"]) < set(new["warnings"])
    restored = await http.post(path + new["id"] + "/filter", json={"read_only_filter": True})
    assert restored.status_code == 201 and restored.json()["warnings"] == old["warnings"]
    assert len(ucp_network[1]) == requests
