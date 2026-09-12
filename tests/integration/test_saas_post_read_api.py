"""Retired POST approval routes have no effects; hosted catalog POSTs need no grants."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import Client
from test_saas_http import _data
from test_saas_user_http import POLICY, Members
from test_saas_user_http import members as members

_SPEC = (Path(__file__).parents[1] / "fixtures" / "cse_read_only_posts.yaml").read_text()
_FUNCTIONS = ["get_market_status", "get_company_info_summery", "get_company_profile", "get_company_info_video"]


async def _setup(members: Members, *, foreign: bool = False) -> dict[str, str]:
    """Publish included POST metadata and an independently keyed tenant-owned channel."""
    http, tenant = (members.second, members.tenants[1]) if foreign else (members.first, members.tenants[0])
    prefix = f"/api/tenants/{tenant}"
    uploaded = await http.post(prefix + "/specs", json={"name": "cse", "content": _SPEC, "read_only_filter": False})
    assert uploaded.status_code == 201 and uploaded.json()["approved_post_reads"] == []
    spec_id = uploaded.json()["id"]
    channel = await http.post(prefix + "/channels", json=POLICY | {"spec_ids": [spec_id]})
    assert channel.status_code == 201
    channel_id = channel.json()["id"]
    key = (await http.post(prefix + "/channels/" + channel_id + "/rotate")).json()
    return {
        "spec": spec_id,
        "prefix": prefix,
        "channel": channel_id,
        "token": key["token"],
        "url": str(http.base_url).rstrip("/") + key["endpoint"],
    }


async def _legacy_grants(members: Members, data: dict[str, str]) -> list[dict[str, object]]:
    """Seed a historical snapshot using retained helpers, never the retired public route."""
    store, importer = members.app.state.store, members.app.state.admin.importer
    tenant = data["prefix"].rsplit("/", 1)[1]
    previous = await store.get_spec(tenant, data["spec"])
    previous.approved_post_reads = await importer.select_post_reads(previous, ["cse." + name for name in _FUNCTIONS])
    updated, _ = await store.refresh_spec(
        tenant, previous.id, await importer.refilter(previous, False), update_channels=True
    )
    data["spec"] = updated.id
    return [permit.model_dump() for permit in updated.approved_post_reads]


async def _execute(data: dict[str, str], function: str, *, succeeds: bool, denial: str = "security") -> str | None:
    """Exercise actual Monty, authoritative broker lookup, and exact-source replay."""
    args = {} if function in {"get_market_status", "get_safe_market_data"} else {"json_body": {"symbol": "SYN"}}
    async with Client(data["url"], auth=data["token"]) as client:
        result = await _data(
            client,
            "execute_code",
            {
                "code": f'result = await call_tool("cse.{function}", inputs)',
                "description": "Included POST",
                "inputs": args,
            },
        )
        assert result["success"] is succeeds
        if succeeds:
            assert result["data"] == {"synthetic": True}
            replay = await _data(client, "run_cached_code", {"cache_id": result["cache_id"], "params": args})
            assert replay["success"] and replay["data"] == result["data"]
            return str(result["cache_id"])
        assert result["error_type"] == denial
        return None


async def test_catalog_posts_execute_forms_and_replay_without_approval(members: Members) -> None:
    """Included POSTs execute without grants while ordinary hosted write permissions stay disabled."""
    data = await _setup(members)
    network = AsyncMock(return_value=httpx.Response(200, json={"synthetic": True}))
    with patch("gryphon.security.broker.NetworkClient.request", network):
        for function in [*_FUNCTIONS, "get_safe_market_data"]:
            await _execute(data, function, succeeds=True)
        assert network.await_count == 10
    broker = members.app.state.runtimes._runtimes[data["channel"]].broker
    assert broker is not None and broker._config.allow_catalog_posts
    assert not broker._config.allow_writes and broker._config.allowed_write_operations == []
    assert broker._config.allowed_read_only_post_operations == []
    assert members.app.state.admin.base.allowed_read_only_post_operations == []
    events = (await members.admin.get("/api/audit")).json()["items"]
    assert not any(event["event"] == "post_reads_updated" for event in events)


@pytest.mark.parametrize("role", ["admin", "first", "second", "anonymous"])
async def test_retired_post_read_routes_return_404_for_every_role_without_effects(members: Members, role: str) -> None:
    """Neither real nor forged authority reaches the retired route or any selection worker."""
    data = await _setup(members)
    path = data["prefix"] + "/specs/" + data["spec"] + "/post-reads"
    before_specs = (await members.admin.get(data["prefix"] + "/specs")).json()
    before_channels = (await members.admin.get(data["prefix"] + "/channels")).json()
    before_audit = (await members.admin.get("/api/audit")).json()
    async with httpx.AsyncClient(base_url=members.admin.base_url) as anonymous:
        client = {"admin": members.admin, "first": members.first, "second": members.second, "anonymous": anonymous}[
            role
        ]
        importer = members.app.state.admin.importer
        with (
            patch.object(importer, "post_read_candidates", AsyncMock()) as candidates,
            patch.object(importer, "select_post_reads", AsyncMock()) as selection,
        ):
            assert (await client.get(path)).status_code == 404
            for body in [{"functions": ["cse.get_market_status"]}, {"functions": []}, {"actor_id": "bootstrap"}]:
                assert (await client.post(path, json=body)).status_code == 404
            assert (await client.post(path, json={}, headers={"x-csrf-token": "wrong"})).status_code == 404
        candidates.assert_not_awaited()
        selection.assert_not_awaited()
    assert (await members.admin.get(data["prefix"] + "/specs")).json() == before_specs
    assert (await members.admin.get(data["prefix"] + "/channels")).json() == before_channels
    assert (await members.admin.get("/api/audit")).json() == before_audit


async def test_automatic_posts_still_require_exact_tenant_binding_and_owned_recipes(members: Members) -> None:
    """Identical names in another tenant neither grant binding authority nor expose cached source."""
    own = await _setup(members)
    foreign = await _setup(members, foreign=True)
    before = (await members.second.get(foreign["prefix"] + "/channels")).json()
    payload = POLICY | {"spec_ids": [own["spec"]]}
    assert (await members.second.post(foreign["prefix"] + "/channels", json=payload)).status_code == 404
    assert (
        await members.second.patch(
            foreign["prefix"] + "/channels/" + foreign["channel"], json=payload | {"enabled": True}
        )
    ).status_code == 404
    assert (await members.second.get(foreign["prefix"] + "/channels")).json() == before
    network = AsyncMock(return_value=httpx.Response(200, json={"synthetic": True}))
    with patch("gryphon.security.broker.NetworkClient.request", network):
        cache_id = await _execute(own, _FUNCTIONS[0], succeeds=True)
        async with Client(foreign["url"], auth=foreign["token"]) as client:
            replay = await _data(client, "run_cached_code", {"cache_id": cache_id, "params": {}})
            assert not replay["success"] and network.await_count == 2
        await _execute(foreign, _FUNCTIONS[0], succeeds=True)
    assert network.await_count == 4


async def test_legacy_grants_survive_filter_but_changed_document_clears_compatibility_metadata(
    members: Members,
) -> None:
    """Historical grants preserve filtered inclusion, not an approval prerequisite for unfiltered POSTs."""
    data = await _setup(members)
    grants = await _legacy_grants(members, data)
    path = data["prefix"] + "/specs/" + data["spec"]
    filtered = await members.first.post(path + "/filter", json={"read_only_filter": True, "update_channels": True})
    assert filtered.status_code == 201
    current = filtered.json()
    assert current["approved_post_reads"] == grants
    assert current["diagnostics"]["available_operations"] == 5
    network = AsyncMock(return_value=httpx.Response(200, json={"synthetic": True}))
    with patch("gryphon.security.broker.NetworkClient.request", network):
        await _execute(data, _FUNCTIONS[0], succeeds=True)
    assert network.await_count == 2
    document = (await members.first.get(data["prefix"] + "/specs/" + current["id"])).json()["document"]
    document["info"]["version"] = "2"
    changed = await members.first.post(
        data["prefix"] + "/specs/" + current["id"] + "/refresh",
        json={"content": json.dumps(document), "update_channels": True},
    )
    assert changed.status_code == 201 and changed.json()["approved_post_reads"] == []
    assert changed.json()["diagnostics"]["available_operations"] == 1
    with patch("gryphon.security.broker.NetworkClient.request", AsyncMock()) as network:
        await _execute(data, _FUNCTIONS[0], succeeds=False, denial="not_found")
    network.assert_not_awaited()


@pytest.mark.parametrize(
    "functions", [None, "cse.get_market_status", {}, [1], ["cse.get_market_status"] * 2, ["missing"]]
)
async def test_retired_post_read_selection_rejects_all_payloads_without_mutation(
    members: Members, functions: object
) -> None:
    """Malformed legacy payloads cannot revive a retired mutation route."""
    data = await _setup(members)
    before = (await members.admin.get(data["prefix"] + "/specs")).json()
    response = await members.admin.post(
        data["prefix"] + "/specs/" + data["spec"] + "/post-reads", json={"functions": functions}
    )
    assert response.status_code == 404
    assert (await members.admin.get(data["prefix"] + "/specs")).json() == before


async def test_retired_post_read_publication_never_starts_selection(members: Members) -> None:
    """Even a revoked submitting session cannot reach a legacy worker or change channel revisions."""
    data = await _setup(members)
    before_specs = (await members.first.get(data["prefix"] + "/specs")).json()
    before_channels = (await members.first.get(data["prefix"] + "/channels")).json()
    assert (await members.admin.post("/api/logout")).status_code == 200
    with patch.object(members.app.state.admin.importer, "select_post_reads", AsyncMock()) as selection:
        response = await members.admin.post(
            data["prefix"] + "/specs/" + data["spec"] + "/post-reads", json={"functions": ["cse.get_market_status"]}
        )
    assert response.status_code == 404
    selection.assert_not_awaited()
    assert (await members.first.get(data["prefix"] + "/specs")).json() == before_specs
    assert (await members.first.get(data["prefix"] + "/channels")).json() == before_channels


async def test_post_read_grants_cannot_be_injected_through_ordinary_spec_mutations(members: Members) -> None:
    """Tenant editors cannot inject legacy policy metadata through ordinary import fields."""
    data = await _setup(members)
    fields: dict[str, object] = {
        "approved_post_reads": [
            {"server_name": "cse", "base_url": "https://market.example/api", "path": "/marketStatus"}
        ]
    }
    path = data["prefix"] + "/specs/" + data["spec"]
    assert (
        await members.first.post(data["prefix"] + "/specs", json={"name": "forged", "content": _SPEC} | fields)
    ).status_code == 400
    assert (await members.first.post(path + "/refresh", json={"content": _SPEC} | fields)).status_code == 400
    assert (await members.first.post(path + "/filter", json={"read_only_filter": False} | fields)).status_code == 400


async def test_disabled_tenant_cannot_reach_retired_post_review_or_selection(members: Members) -> None:
    """Disabled resources do not restore retired routes, even for platform administrators."""
    data = await _setup(members)
    assert (await members.admin.patch(data["prefix"], json={"enabled": False})).status_code == 200
    importer = members.app.state.admin.importer
    path = data["prefix"] + "/specs/" + data["spec"] + "/post-reads"
    with (
        patch.object(importer, "post_read_candidates", AsyncMock()) as candidates,
        patch.object(importer, "select_post_reads", AsyncMock()) as selection,
    ):
        assert (await members.admin.get(path)).status_code == 404
        assert (await members.admin.post(path, json={"functions": []})).status_code == 404
        assert (await members.first.get(path)).status_code == 404
    candidates.assert_not_awaited()
    selection.assert_not_awaited()
