"""Offline canonical POST review, scoped policy, and immutable approval regressions."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from test_saas_store import store as store

from gryphon.errors import CapacityError, InputValidationError, SecurityViolationError
from gryphon.models import Channel, ReadOnlyPostOperation, SaaSSpec
from gryphon.saas_catalog import approved_spec_config, compile_catalog
from gryphon.saas_spec_import import SpecImporter
from gryphon.saas_spec_versions import new_spec

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.saas_store import SaaSStore

_CONTENT = (Path(__file__).parents[1] / "fixtures" / "cse_read_only_posts.yaml").read_text()
_LIMIT = 100_000
_TENANT = "a" * 32


async def _spec(config: GryphonConfig, *, filtered: bool = True) -> SaaSSpec:
    """Create a validated synthetic saved snapshot without touching operator stores."""
    imported = await SpecImporter(config, _LIMIT).load("cse", content=_CONTENT, read_only_filter=filtered)
    return new_spec(_TENANT, "cse", imported.document, _LIMIT, imported)


async def test_post_read_candidates_ignore_hints_and_include_both_form_types(gryphon_config: GryphonConfig) -> None:
    """Filter-off and persuasive summaries do not grant execution authority."""
    spec = await _spec(gryphon_config, filtered=False)
    importer = SpecImporter(gryphon_config, _LIMIT)
    with patch("gryphon.security.network.NetworkClient.request") as network:
        rows = await importer.post_read_candidates(spec)
    network.assert_not_called()
    assert len(rows) == 5
    assert {row["method"] for row in rows} == {"POST"}
    assert all(not row["approved"] and not row["operator_approved"] for row in rows)
    assert {row["function_name"] for row in rows} >= {"cse.get_company_profile", "cse.get_company_info_video"}
    assert spec.approved_post_reads == []
    assert await importer.select_post_reads(spec, []) == []


async def test_post_read_selection_uses_parser_effective_destination(gryphon_config: GryphonConfig) -> None:
    """Canonical operation overrides, not the browser or root URL, determine the exact grant."""
    spec = await _spec(gryphon_config)
    spec.document["paths"]["/marketStatus"]["post"]["servers"] = [{"url": "https://cdn.example/data"}]
    selected = await SpecImporter(gryphon_config, _LIMIT).select_post_reads(spec, ["cse.get_market_status"])
    assert selected == [
        ReadOnlyPostOperation(server_name="cse", base_url="https://cdn.example/data", path="/marketStatus")
    ]
    assert gryphon_config.allowed_read_only_post_operations == []


@pytest.mark.parametrize(
    "functions",
    [
        None,
        {},
        "cse.get_market_status",
        [1],
        ["stale"],
        ["cse.get_company_data_by_put"],
        ["cse.get_market_status", "cse.get_market_status"],
        ["x"] * 1001,
    ],
)
async def test_post_read_invalid_or_stale_selection_fails_closed(
    gryphon_config: GryphonConfig, functions: object
) -> None:
    """Only unique, bounded, currently parsed POST function names can be selected."""
    spec = await _spec(gryphon_config)
    with pytest.raises(InputValidationError):
        await SpecImporter(gryphon_config, _LIMIT).select_post_reads(spec, functions)


async def test_post_read_operator_grants_are_inherited_not_stored_or_revoked(gryphon_config: GryphonConfig) -> None:
    """Empty browser selection cannot revoke the deployment operator's independent permit."""
    importer = SpecImporter(gryphon_config, _LIMIT)
    spec = await _spec(gryphon_config)
    permit = (await importer.select_post_reads(spec, ["cse.get_market_status"]))[0]
    gryphon_config.allowed_read_only_post_operations = [permit]
    rows = await importer.post_read_candidates(spec)
    row = next(row for row in rows if row["function_name"] == "cse.get_market_status")
    assert row["approved"] and row["operator_approved"]
    assert await importer.select_post_reads(spec, [row["function_name"]]) == []
    assert await importer.select_post_reads(spec, []) == []
    assert gryphon_config.allowed_read_only_post_operations == [permit]


async def test_post_read_filter_and_identical_refresh_keep_grants_changed_digest_clears(
    gryphon_config: GryphonConfig,
) -> None:
    """Even a description-only document edit requires re-review; filter preferences do not."""
    importer = SpecImporter(gryphon_config, _LIMIT)
    spec = await _spec(gryphon_config)
    spec.approved_post_reads = await importer.select_post_reads(spec, ["cse.get_market_status"])
    for filtered, count in [(True, 2), (False, 7)]:
        imported = await importer.refilter(spec, filtered)
        assert imported.approved_post_reads == spec.approved_post_reads
        assert imported.diagnostics and imported.diagnostics.available_operations == count
    same = await importer.load("cse", content=_CONTENT, previous=spec)
    assert same.approved_post_reads == spec.approved_post_reads
    changed = copy.deepcopy(spec.document)
    changed["info"]["description"] = "Reviewed document changed"
    fresh = await importer.load("cse", content=json.dumps(changed), previous=spec)
    assert fresh.approved_post_reads == []
    assert fresh.diagnostics and fresh.diagnostics.available_operations == 1


async def test_post_read_scoped_config_validates_tenant_before_combining(gryphon_config: GryphonConfig) -> None:
    """Matching namespaces and URLs in another tenant cannot inherit scoped execution grants."""
    spec = await _spec(gryphon_config)
    importer = SpecImporter(gryphon_config, _LIMIT)
    spec.approved_post_reads = await importer.select_post_reads(spec, ["cse.get_market_status"])
    channel = Channel(id="b" * 32, tenant_id=_TENANT, name="test", spec_ids=[spec.id], created_at=0)
    config = approved_spec_config(gryphon_config, channel, [spec])
    assert config.allowed_read_only_post_operations == spec.approved_post_reads
    assert gryphon_config.allowed_read_only_post_operations == []
    assert not config.allow_writes and not config.allowed_write_operations
    registry = await compile_catalog(gryphon_config, channel, [spec])
    assert registry.get_endpoint("cse", "get_market_status").read_only_post
    foreign = spec.model_copy(update={"tenant_id": "c" * 32})
    with pytest.raises(SecurityViolationError):
        approved_spec_config(gryphon_config, channel, [foreign])
    assert gryphon_config.allowed_read_only_post_operations == []


async def test_post_read_approval_only_versions_revise_bindings_and_audit(
    store: SaaSStore, gryphon_config: GryphonConfig
) -> None:
    """Grant/revoke changes immutable policy even when unfiltered counts and document hash match."""
    tenant = await store.create_tenant("Review")
    importer = SpecImporter(gryphon_config, _LIMIT)
    imported = await importer.load("cse", content=_CONTENT, read_only_filter=False)
    old = await store.create_spec(tenant.id, "cse", imported.document, imported=imported)
    channel = await store.create_channel(tenant.id, "Bound", spec_ids=[old.id])
    old.approved_post_reads = await importer.select_post_reads(old, ["cse.get_market_status"])
    granted, channels = await store.refresh_spec(
        tenant.id, old.id, await importer.refilter(old, False), update_channels=True
    )
    assert granted.sha256 == old.sha256 and granted.parent_id == old.id
    assert channels[0].revision == channel.revision + 1 and channels[0].spec_ids == [granted.id]
    assert (await store.get_spec(tenant.id, old.id)).approved_post_reads == []
    granted.approved_post_reads = []
    revoked, channels = await store.refresh_spec(
        tenant.id, granted.id, await importer.refilter(granted, False), update_channels=True
    )
    assert revoked.parent_id == granted.id and not revoked.approved_post_reads
    assert channels[0].revision == channel.revision + 2
    assert sum(event.event == "post_reads_updated" for event in await store.list_audit(tenant.id)) == 2


async def test_post_read_non_literal_routes_are_excluded_and_admission_is_shared(gryphon_config: GryphonConfig) -> None:
    """Parameterized POST paths have no literal permit representation and cannot be approved."""
    spec = await _spec(gryphon_config)
    spec.document["paths"]["/{symbol}"] = spec.document["paths"].pop("/marketStatus")
    importer = SpecImporter(gryphon_config, _LIMIT)
    assert len(await importer.post_read_candidates(spec)) == 4
    with pytest.raises(InputValidationError):
        await importer.select_post_reads(spec, ["cse.get_market_status"])
    async with importer._lock:
        with pytest.raises(CapacityError):
            await importer.post_read_candidates(spec)
        with pytest.raises(CapacityError):
            await importer.select_post_reads(spec, [])


async def test_post_read_review_handles_twenty_five_literal_form_routes(gryphon_config: GryphonConfig) -> None:
    """A synthetic CSE-sized catalog exposes every one of 23 URL-encoded and two multipart POSTs."""
    spec = await _spec(gryphon_config)
    paths = {"/allSecurityCode": spec.document["paths"]["/allSecurityCode"]}
    for index in range(25):
        template = "/companyInfoSummery" if index < 23 else "/companyProfile"
        operation = copy.deepcopy(spec.document["paths"][template]["post"])
        operation["operationId"] = f"read{index}"
        paths[f"/read{index}"] = {"post": operation}
    spec.document["paths"] = paths
    importer = SpecImporter(gryphon_config, _LIMIT)
    imported = await importer.load("cse", content=json.dumps(spec.document))
    spec = new_spec(_TENANT, "cse", imported.document, _LIMIT, imported)
    rows = await importer.post_read_candidates(spec)
    assert len(rows) == 25
    spec.approved_post_reads = await importer.select_post_reads(spec, [row["function_name"] for row in rows])
    approved = await importer.refilter(spec, True)
    assert approved.diagnostics and approved.diagnostics.available_operations == 26
    assert len(approved.approved_post_reads) == 25


async def test_post_read_remote_refresh_hashes_normalized_servers_before_retaining(
    gryphon_config: GryphonConfig,
) -> None:
    """Saved absolute server normalization does not itself erase grants on unchanged URL refresh."""
    spec = await _spec(gryphon_config)
    document = copy.deepcopy(spec.document)
    document["servers"] = [{"url": "/api"}]
    importer = SpecImporter(gryphon_config, _LIMIT)
    location = "https://market.example/spec.json"
    with patch(
        "gryphon.saas_spec_import.NetworkClient.request", AsyncMock(return_value=httpx.Response(200, json=document))
    ):
        imported = await importer.load("cse", url=location)
        spec = new_spec(_TENANT, "cse", imported.document, _LIMIT, imported)
        spec.approved_post_reads = await importer.select_post_reads(spec, ["cse.get_market_status"])
        same = await importer.load("cse", url=location, previous=spec)
    assert same.approved_post_reads == spec.approved_post_reads
    assert same.source_type == "openapi_url" and same.source_url == location
    document["info"]["version"] = "2"
    with patch(
        "gryphon.saas_spec_import.NetworkClient.request", AsyncMock(return_value=httpx.Response(200, json=document))
    ):
        changed = await importer.load("cse", url=location, previous=spec)
    assert not changed.approved_post_reads
    assert changed.diagnostics and changed.diagnostics.available_operations == 1
