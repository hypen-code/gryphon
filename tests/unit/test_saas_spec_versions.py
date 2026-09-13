"""Atomic immutable specification updates in disposable tenant stores."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from test_saas_store import store as store

from gryphon.errors import ConflictError, SaaSNotFoundError, SaaSQuotaError, SaaSStoreError
from gryphon.models import SaaSSpec, SpecImport

if TYPE_CHECKING:
    from gryphon.saas_store import SaaSStore


async def test_spec_refresh_preserves_old_snapshot_and_revises_exact_bindings(store: SaaSStore) -> None:
    """A successor advances exact parent bindings without modifying old or unrelated state."""
    tenant = await store.create_tenant("one")
    old = await store.create_spec(tenant.id, "api", {"version": 1})
    other = await store.create_spec(tenant.id, "other", {})
    bound = await store.create_channel(tenant.id, "bound", spec_ids=[other.id, old.id])
    untouched = await store.create_channel(tenant.id, "untouched", spec_ids=[other.id])
    updated, channels = await store.refresh_spec(
        tenant.id, old.id, SpecImport(document={"version": 2}), update_channels=True
    )
    assert updated.parent_id == old.id and updated.id != old.id and updated.name == old.name
    assert await store.get_spec(tenant.id, old.id) == old
    current = await store.get_channel(tenant.id, bound.id)
    assert current.spec_ids == [other.id, updated.id] and current.revision == bound.revision + 1
    assert channels == [current]
    assert await store.get_channel(tenant.id, untouched.id) == untouched
    assert {event.event for event in await store.list_audit(tenant.id)} >= {"spec_created", "channel_updated"}


async def test_spec_refresh_unchanged_is_idempotent_without_quota_or_channel_drift(store: SaaSStore) -> None:
    """Identical content and provenance consume neither retained-version quota nor revisions."""
    tenant = await store.create_tenant("one")
    imported = SpecImport(document={"version": 1}, source_type="openapi_url", source_url="https://example.com/api.json")
    old = await store.create_spec(tenant.id, "api", imported.document, imported=imported)
    channel = await store.create_channel(tenant.id, "bound", spec_ids=[old.id])
    store._max_specs = 1
    updated, channels = await store.refresh_spec(tenant.id, old.id, imported, update_channels=True)
    assert updated == old and channels == []
    assert await store.get_channel(tenant.id, channel.id) == channel
    assert len(await store.list_specs(tenant.id)) == 1


async def test_spec_refresh_can_leave_bindings_pinned_and_rejects_stale_parent(store: SaaSStore) -> None:
    """Opt-out bindings remain pinned and an already superseded parent cannot branch."""
    tenant = await store.create_tenant("one")
    old = await store.create_spec(tenant.id, "api", {})
    channel = await store.create_channel(tenant.id, "bound", spec_ids=[old.id])
    new, channels = await store.refresh_spec(tenant.id, old.id, SpecImport(document={"n": 1}), update_channels=False)
    assert channels == [] and new.parent_id == old.id
    assert await store.get_channel(tenant.id, channel.id) == channel
    with pytest.raises(ConflictError):
        await store.refresh_spec(tenant.id, old.id, SpecImport(document={"n": 2}), update_channels=True)


async def test_spec_refresh_is_tenant_scoped_and_provenance_is_immutable(store: SaaSStore) -> None:
    """Refresh cannot steal a foreign spec or convert upload provenance into a remote source."""
    tenant = await store.create_tenant("one")
    foreign = await store.create_tenant("two")
    old = await store.create_spec(tenant.id, "api", {})
    with pytest.raises(SaaSNotFoundError):
        await store.refresh_spec(foreign.id, old.id, SpecImport(document={"n": 1}), update_channels=True)
    with pytest.raises(ConflictError):
        await store.refresh_spec(
            tenant.id,
            old.id,
            SpecImport(document={}, source_type="ucp_url", source_url="https://example.com"),
            update_channels=True,
        )


@pytest.mark.parametrize("failure", ["quota", "audit"])
async def test_spec_refresh_failure_rolls_back_version_and_bindings(store: SaaSStore, failure: str) -> None:
    """Quota or audit failures roll back the whole successor publication transaction."""
    tenant = await store.create_tenant("one")
    old = await store.create_spec(tenant.id, "api", {})
    channel = await store.create_channel(tenant.id, "bound", spec_ids=[old.id])
    if failure == "quota":
        store._max_specs = 1
        with pytest.raises(SaaSQuotaError):
            await store.refresh_spec(tenant.id, old.id, SpecImport(document={"n": 1}), update_channels=True)
    else:
        with (
            patch.object(store, "_audit", AsyncMock(side_effect=SaaSStoreError("private"))),
            pytest.raises(SaaSStoreError),
        ):
            await store.refresh_spec(tenant.id, old.id, SpecImport(document={"n": 1}), update_channels=True)
    assert await store.get_channel(tenant.id, channel.id) == channel
    assert await store.list_specs(tenant.id) == [old]


async def test_spec_concurrent_refresh_publishes_only_one_successor(store: SaaSStore) -> None:
    """Concurrent changed refreshes serialize into one commit and one safe conflict."""
    tenant = await store.create_tenant("one")
    old = await store.create_spec(tenant.id, "api", {"version": 0})
    channel = await store.create_channel(tenant.id, "bound", spec_ids=[old.id])
    results = await asyncio.gather(
        *(
            store.refresh_spec(tenant.id, old.id, SpecImport(document={"version": n}), update_channels=True)
            for n in (1, 2)
        ),
        return_exceptions=True,
    )
    successes = [result for result in results if isinstance(result, tuple)]
    assert len(successes) == 1 and sum(isinstance(result, ConflictError) for result in results) == 1
    successor, changed = successes[0]
    current = await store.get_channel(tenant.id, channel.id)
    assert current.spec_ids == [successor.id] and current.revision == channel.revision + 1
    assert changed == [current] and successor.parent_id == old.id
    assert await store.get_spec(tenant.id, old.id) == old
    assert {item.id for item in await store.list_specs(tenant.id)} == {old.id, successor.id}
    events = await store.list_audit(tenant.id)
    assert sum(event.event == "spec_created" for event in events) == 2
    assert sum(event.event == "channel_updated" for event in events) == 1


async def test_spec_legacy_payload_defaults_preserve_bindings_and_refresh(store: SaaSStore) -> None:
    """Pre-provenance JSON rows load with file defaults and retain immutable migration identity."""
    tenant = await store.create_tenant("legacy")
    original = await store.create_spec(tenant.id, "api", {"version": 1})
    channel = await store.create_channel(tenant.id, "bound", spec_ids=[original.id])
    legacy = original.model_dump(include={"id", "tenant_id", "name", "document", "sha256", "created_at"})
    async with store._db.transaction():
        await store._db.execute(
            "UPDATE saas_specs SET payload=? WHERE tenant_id=? AND id=?",
            (json.dumps(legacy), tenant.id, original.id),
        )
    loaded = await store.get_spec(tenant.id, original.id)
    assert loaded == original == SaaSSpec.model_validate(legacy)
    assert loaded.source_type == "file" and loaded.source_url is None and loaded.parent_id is None
    assert loaded.diagnostics is None and loaded.warnings == []
    assert await store.get_channel(tenant.id, channel.id) == channel
    successor, changed = await store.refresh_spec(
        tenant.id, loaded.id, SpecImport(document={"version": 2}), update_channels=True
    )
    assert successor.source_type == "file" and successor.parent_id == loaded.id
    assert changed[0].spec_ids == [successor.id]
    assert await store.get_spec(tenant.id, loaded.id) == original
