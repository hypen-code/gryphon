"""Whole-lineage deletion regression coverage using disposable SQLite stores only."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from test_saas_store import store as store

from gryphon.errors import ConflictError, SaaSNotFoundError, SaaSQuotaError, SaaSStoreError, SaaSValidationError
from gryphon.models import (
    AuditActor,
    AuditEvent,
    MCPBinding,
    ReadOnlyPostOperation,
    SaaSSpec,
    SpecDiagnostics,
    SpecImport,
)
from gryphon.saas_audit import audit_actor

if TYPE_CHECKING:
    from gryphon.saas_store import SaaSStore


async def _snapshot(store: SaaSStore) -> list[object]:
    """Capture isolated control metadata to prove rejected operations have no side effects."""
    async with store._db.transaction():
        return [
            await store._db.execute("SELECT * FROM saas_specs ORDER BY tenant_id,id"),
            await store._db.execute("SELECT * FROM saas_channels ORDER BY tenant_id,id"),
            await store._db.execute("SELECT * FROM saas_bindings ORDER BY tenant_id,channel_id,spec_id"),
            await store._db.execute("SELECT * FROM saas_audit ORDER BY id"),
        ]


async def _save_fixture(store: SaaSStore, item: SaaSSpec) -> None:
    """Write deliberately malformed lineage fixtures only in the temporary test database."""
    async with store._db.transaction():
        await store._db.execute(
            "UPDATE saas_specs SET payload=? WHERE tenant_id=? AND id=?",
            (item.model_dump_json(), item.tenant_id, item.id),
        )


@pytest.mark.parametrize("target", [0, 1, 2])
async def test_spec_delete_whole_lineage_preserves_other_roots_and_tenants(store: SaaSStore, target: int) -> None:
    """Any version selects its entire lineage, including older pins, never unrelated same-name roots."""
    tenant = await store.create_tenant("one")
    foreign = await store.create_tenant("two")
    root = await store.create_spec(tenant.id, "api", {})
    second, _ = await store.refresh_spec(tenant.id, root.id, SpecImport(document={"v": 1}))
    latest, _ = await store.refresh_spec(tenant.id, second.id, SpecImport(document={"v": 2}))
    versions = [root, second, latest]
    other = await store.create_spec(tenant.id, root.name, {})
    foreign_spec = await store.create_spec(foreign.id, root.name, {})
    foreign_channel = await store.create_channel(foreign.id, "foreign", spec_ids=[foreign_spec.id])
    bound = await store.create_channel(
        tenant.id,
        "bound",
        spec_ids=[root.id, other.id, latest.id],
        allowed_imports=["math"],
        sandbox_mode="docker",
        include_function_summaries=True,
    )
    key = await store.rotate_key(tenant.id, bound.id)
    bound = await store.get_channel(tenant.id, bound.id)
    disabled = await store.create_channel(tenant.id, "disabled", spec_ids=[second.id])
    disabled = await store.update_channel(tenant.id, disabled.id, enabled=False)
    untouched = await store.create_channel(tenant.id, "untouched", spec_ids=[other.id])
    await store.record_usage(tenant.id, bound.id, "get_run", "success", 1)
    usage = await store.list_usage(tenant.id)
    preview = await store.preview_spec_deletion(tenant.id, versions[target].id)
    assert preview.name == root.name and preview.specification_id == root.id
    assert preview.spec_id == versions[target].id and preview.version_count == 3
    assert preview.version_ids == sorted(item.id for item in versions)
    assert [item.id for item in preview.channels] == sorted([bound.id, disabled.id])
    assert re.fullmatch(r"[0-9a-f]{64}", preview.confirmation_token)
    deleted, changed = await store.delete_spec(tenant.id, preview.spec_id, root.name, preview.confirmation_token)
    assert deleted == preview and {item.id for item in changed} == {bound.id, disabled.id}
    assert await store.get_channel(tenant.id, bound.id) == bound.model_copy(
        update={"spec_ids": [other.id], "revision": bound.revision + 1}
    )
    assert await store.get_channel(tenant.id, disabled.id) == disabled.model_copy(
        update={"spec_ids": [], "revision": disabled.revision + 1}
    )
    assert await store.get_channel(tenant.id, untouched.id) == untouched
    assert await store.lookup_key(key) is not None and await store.list_usage(tenant.id) == usage
    assert await store.list_specs(tenant.id) == [other]
    assert await store.list_specs(foreign.id) == [foreign_spec]
    assert await store.get_channel(foreign.id, foreign_channel.id) == foreign_channel
    async with store._db.transaction():
        assert await store._db.execute("PRAGMA foreign_key_check") == []


@pytest.mark.parametrize(
    "imported",
    [
        SpecImport(document={}),
        SpecImport(document={}, source_type="openapi_url", source_url="https://example.com/api"),
        SpecImport(document={}, source_type="ucp_url", source_url="https://example.com/ucp", source_transport="rest"),
        SpecImport(
            document={},
            source_type="ucp_url",
            source_url="https://example.com/mcp",
            source_transport="mcp",
            resolved_profile_url="https://example.com/.well-known/ucp",
            resolved_endpoint="https://example.com/mcp",
            mcp_bindings={
                "get_cart": MCPBinding(
                    endpoint="https://example.com/mcp", tool_name="get_cart", tool_fingerprint="a" * 64
                )
            },
            approved_post_reads=[
                ReadOnlyPostOperation(server_name="api", base_url="https://example.com", path="/cart")
            ],
        ),
    ],
)
async def test_spec_delete_all_source_kinds_and_metadata_versions(store: SaaSStore, imported: SpecImport) -> None:
    """File, OpenAPI, REST UCP and native UCP provenance do not restrict logical deletion."""
    tenant = await store.create_tenant("one")
    root = await store.create_spec(tenant.id, "api", imported.document, imported=imported)
    metadata = imported.model_copy(
        update={"read_only_filter": False, "diagnostics": SpecDiagnostics(), "warnings": ["x"]}
    )
    latest, _ = await store.refresh_spec(tenant.id, root.id, metadata)
    assert latest.sha256 == root.sha256 and latest.id != root.id
    preview = await store.preview_spec_deletion(tenant.id, latest.id)
    assert preview.version_count == 2 and preview.channels == []
    assert await store.preview_spec_deletion(tenant.id, latest.id) == preview
    _, channels = await store.delete_spec(tenant.id, latest.id, root.name, preview.confirmation_token)
    assert channels == [] and await store.list_specs(tenant.id) == []


@pytest.mark.parametrize("name", [None, True, 1, [], {}, "", " api ", "API", "api\n", "x" * 129])
async def test_spec_delete_requires_exact_bounded_typed_name(store: SaaSStore, name: object) -> None:
    """Malformed, whitespace-modified and case-modified names never mutate or audit."""
    tenant = await store.create_tenant("one")
    spec = await store.create_spec(tenant.id, "api", {})
    preview = await store.preview_spec_deletion(tenant.id, spec.id)
    before = await _snapshot(store)
    with pytest.raises(SaaSValidationError):
        await store.delete_spec(tenant.id, spec.id, name, preview.confirmation_token)
    assert await _snapshot(store) == before


@pytest.mark.parametrize("token", [None, True, 42, [], {}, "", "x" * 64, "a" * 63, "a" * 65, "A" * 64, "a" * 64 + "\n"])
async def test_spec_delete_malformed_token_is_noop(store: SaaSStore, token: object) -> None:
    """Confirmation digests require exactly 64 lowercase hex characters without coercion."""
    tenant = await store.create_tenant("one")
    spec = await store.create_spec(tenant.id, "api", {})
    before = await _snapshot(store)
    with pytest.raises(SaaSValidationError):
        await store.delete_spec(tenant.id, spec.id, spec.name, token)
    assert await _snapshot(store) == before


@pytest.mark.parametrize("change", ["refresh", "binding", "rename", "config", "revision", "key", "requested", "wrong"])
async def test_spec_delete_stale_preview_requires_reconfirmation(store: SaaSStore, change: str) -> None:
    """Every relevant lineage or channel change invalidates a previously issued digest."""
    tenant = await store.create_tenant("one")
    spec = await store.create_spec(tenant.id, "api", {})
    latest, _ = await store.refresh_spec(tenant.id, spec.id, SpecImport(document={"v": 1}))
    channel = await store.create_channel(tenant.id, "bound", spec_ids=[spec.id])
    preview = await store.preview_spec_deletion(tenant.id, spec.id)
    target, token = spec.id, preview.confirmation_token
    if change == "refresh":
        await store.refresh_spec(tenant.id, latest.id, SpecImport(document={"v": 2}))
    elif change == "binding":
        await store.create_channel(tenant.id, "new", spec_ids=[latest.id])
    elif change == "rename":
        await store.update_channel(tenant.id, channel.id, name="renamed")
    elif change == "config":
        channel.include_function_summaries = True
        async with store._db.transaction():
            await store._db.execute(
                "UPDATE saas_channels SET payload=? WHERE id=?", (channel.model_dump_json(), channel.id)
            )
    elif change == "revision":
        await store.update_channel(tenant.id, channel.id)
    elif change == "key":
        await store.rotate_key(tenant.id, channel.id)
    elif change == "requested":
        target = latest.id
    else:
        token = "0" * 64
    before = await _snapshot(store)
    with pytest.raises(ConflictError):
        await store.delete_spec(tenant.id, target, spec.name, token)
    assert await _snapshot(store) == before


@pytest.mark.parametrize("audit_limit", [1, 2, 5])
async def test_spec_delete_audit_snapshot_respects_retention_quota(store: SaaSStore, audit_limit: int) -> None:
    """Keep prior history within quota and the attributed deletion event under normal pruning."""
    store._max_audit = audit_limit
    tenant = await store.create_tenant("one")
    spec = await store.create_spec(tenant.id, "api", {})
    channel = await store.create_channel(tenant.id, "bound", spec_ids=[spec.id])
    before = await store.list_audit(tenant.id)
    actor = AuditActor(id="verified", username="operator", name="Operator", kind="user", display_source="snapshot")
    preview = await store.preview_spec_deletion(tenant.id, spec.id)
    with audit_actor(actor):
        await store.delete_spec(tenant.id, spec.id, spec.name, preview.confirmation_token)
    events = await store.list_audit(tenant.id)
    assert len(events) == audit_limit
    assert events[2:] == before[: max(0, audit_limit - 2)]
    assert events[0].event == "spec_deleted" and events[0].spec_id == spec.id
    assert events[0].actor == actor
    if audit_limit > 1:
        assert events[1].actor == actor
        assert events[1].event == "channel_updated" and events[1].channel_id == channel.id
        assert events[1].spec_id is None


@pytest.mark.parametrize("failure", ["channel_updated", "spec_deleted"])
async def test_spec_delete_audit_failure_rolls_back_all_mutations(store: SaaSStore, failure: str) -> None:
    """Failures after audit insertion and pruning restore old history and every resource mutation."""
    tenant = await store.create_tenant("one")
    root = await store.create_spec(tenant.id, "api", {})
    latest, _ = await store.refresh_spec(tenant.id, root.id, SpecImport(document={"v": 1}))
    await store.create_channel(tenant.id, "bound", spec_ids=[root.id, latest.id])
    preview = await store.preview_spec_deletion(tenant.id, root.id)
    before, original = await _snapshot(store), store._audit
    before_ids = {item.id for item in await store.list_audit(tenant.id)}
    store._max_audit = len(before_ids)

    async def fail(
        tenant_id: str,
        event: AuditEvent,
        channel_id: str | None = None,
        *,
        spec_id: str | None = None,
    ) -> None:
        """Inject a failure after actual persistence and pruning to verify transaction rollback."""
        await original(tenant_id, event, channel_id, spec_id=spec_id)
        if event == failure:
            rows = await store._db.execute("SELECT id FROM saas_audit")
            assert len(rows) == store._max_audit
            assert before_ids - {str(row["id"]) for row in rows}
            raise SaaSStoreError("Synthetic audit failure")

    with patch.object(store, "_audit", fail), pytest.raises(SaaSStoreError):
        await store.delete_spec(tenant.id, root.id, root.name, preview.confirmation_token)
    assert await _snapshot(store) == before
    assert await store.preview_spec_deletion(tenant.id, root.id) == preview


async def test_spec_delete_quota_freed_repeated_and_foreign_delete_not_found(store: SaaSStore) -> None:
    """Exact tenant scope and repeated deletion are 404s; successful deletion frees retained quota."""
    tenant = await store.create_tenant("one")
    foreign = await store.create_tenant("two")
    spec = await store.create_spec(tenant.id, "api", {})
    store._max_specs = 1
    with pytest.raises(SaaSQuotaError):
        await store.create_spec(tenant.id, "full", {})
    preview = await store.preview_spec_deletion(tenant.id, spec.id)
    before = await _snapshot(store)
    for tenant_id in (foreign.id, "absent"):
        with pytest.raises(SaaSNotFoundError):
            await store.preview_spec_deletion(tenant_id, spec.id)
        with pytest.raises(SaaSNotFoundError):
            await store.delete_spec(tenant_id, spec.id, spec.name, preview.confirmation_token)
    assert await _snapshot(store) == before
    await store.delete_spec(tenant.id, spec.id, spec.name, preview.confirmation_token)
    for version_id in (spec.id, "absent"):
        with pytest.raises(SaaSNotFoundError):
            await store.preview_spec_deletion(tenant.id, version_id)
        with pytest.raises(SaaSNotFoundError):
            await store.delete_spec(tenant.id, version_id, spec.name, preview.confirmation_token)
    assert (await store.create_spec(tenant.id, "free", {})).name == "free"


async def test_spec_delete_disabled_tenant_cleanup_allowed(store: SaaSStore) -> None:
    """Administrative deletion remains possible after tenant disable without re-enabling authority."""
    tenant = await store.create_tenant("one")
    spec = await store.create_spec(tenant.id, " api ", {})
    channel = await store.create_channel(tenant.id, "bound", spec_ids=[spec.id])
    await store.disable_tenant(tenant.id)
    before = await store.get_channel(tenant.id, channel.id)
    preview = await store.preview_spec_deletion(tenant.id, spec.id)
    with pytest.raises(SaaSValidationError):
        await store.delete_spec(tenant.id, spec.id, "api", preview.confirmation_token)
    _, channels = await store.delete_spec(tenant.id, spec.id, " api ", preview.confirmation_token)
    assert channels == [before.model_copy(update={"spec_ids": [], "revision": before.revision + 1})]
    assert not (await store.get_tenant(tenant.id)).enabled


@pytest.mark.parametrize("malformed", ["self", "cycle", "orphan", "foreign", "name"])
async def test_spec_delete_invalid_lineage_fails_closed_bounded(store: SaaSStore, malformed: str) -> None:
    """Cycles, missing or foreign ancestors and inconsistent lineage names cannot trigger deletion."""
    tenant = await store.create_tenant("one")
    root = await store.create_spec(tenant.id, "api", {})
    latest, _ = await store.refresh_spec(tenant.id, root.id, SpecImport(document={"v": 1}))
    if malformed == "self":
        root.parent_id = root.id
    elif malformed == "cycle":
        root.parent_id = latest.id
    elif malformed == "orphan":
        root.parent_id = "missing"
    elif malformed == "foreign":
        foreign = await store.create_tenant("foreign")
        root.parent_id = (await store.create_spec(foreign.id, "api", {})).id
    else:
        root.name = "different"
    await _save_fixture(store, root)
    before = await _snapshot(store)
    async with asyncio.timeout(2):
        with pytest.raises(ConflictError):
            await store.preview_spec_deletion(tenant.id, latest.id)
        with pytest.raises(ConflictError):
            await store.delete_spec(tenant.id, latest.id, root.name, "0" * 64)
    assert await _snapshot(store) == before


async def test_spec_delete_connected_branches_are_included(store: SaaSStore) -> None:
    """Historical branch fixtures remain connected by parent IDs, including sibling descendants."""
    tenant = await store.create_tenant("one")
    root = await store.create_spec(tenant.id, "api", {})
    first, _ = await store.refresh_spec(tenant.id, root.id, SpecImport(document={"v": 1}))
    branch = await store.create_spec(tenant.id, "api", {"v": 2})
    branch.parent_id = root.id
    await _save_fixture(store, branch)
    child, _ = await store.refresh_spec(tenant.id, branch.id, SpecImport(document={"v": 3}))
    preview = await store.preview_spec_deletion(tenant.id, first.id)
    assert preview.version_ids == sorted([root.id, first.id, branch.id, child.id])
    await store.delete_spec(tenant.id, first.id, root.name, preview.confirmation_token)
    assert await store.list_specs(tenant.id) == []


async def test_spec_delete_preview_includes_all_retained_rows_not_listing_page(store: SaaSStore) -> None:
    """Administrative listing limits cannot omit versions or affected channels from deletion."""
    tenant = await store.create_tenant("one")
    root = await store.create_spec(tenant.id, "api", {})
    latest, _ = await store.refresh_spec(tenant.id, root.id, SpecImport(document={"v": 1}))
    for index in range(3):
        await store.create_channel(tenant.id, str(index), spec_ids=[root.id, latest.id])
    store._max_list = 1
    preview = await store.preview_spec_deletion(tenant.id, latest.id)
    assert preview.version_count == 2 and len(preview.channels) == 3
    _, channels = await store.delete_spec(tenant.id, latest.id, root.name, preview.confirmation_token)
    assert len(channels) == 3 and all(not item.spec_ids for item in channels)


async def test_spec_delete_concurrent_requests_only_one_commits(store: SaaSStore) -> None:
    """Serialized transactions prevent duplicate audit or double advancement of channel revisions."""
    tenant = await store.create_tenant("one")
    spec = await store.create_spec(tenant.id, "api", {})
    channel = await store.create_channel(tenant.id, "bound", spec_ids=[spec.id])
    preview = await store.preview_spec_deletion(tenant.id, spec.id)
    results = await asyncio.gather(
        *(store.delete_spec(tenant.id, spec.id, spec.name, preview.confirmation_token) for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(item, tuple) for item in results) == 1
    assert sum(isinstance(item, SaaSNotFoundError) for item in results) == 1
    assert (await store.get_channel(tenant.id, channel.id)).revision == channel.revision + 1
    assert sum(item.event == "spec_deleted" for item in await store.list_audit(tenant.id)) == 1


async def test_spec_delete_mismatched_relational_bindings_fail_closed(store: SaaSStore) -> None:
    """Corrupt payload/binding disagreement is rejected before any cleanup mutation."""
    tenant = await store.create_tenant("one")
    spec = await store.create_spec(tenant.id, "api", {})
    channel = await store.create_channel(tenant.id, "bound", spec_ids=[spec.id])
    channel.spec_ids = []
    async with store._db.transaction():
        await store._db.execute(
            "UPDATE saas_channels SET payload=? WHERE id=?", (channel.model_dump_json(), channel.id)
        )
    before = await _snapshot(store)
    with pytest.raises(ConflictError):
        await store.preview_spec_deletion(tenant.id, spec.id)
    assert await _snapshot(store) == before
