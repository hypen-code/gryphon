"""Atomic whole-lineage specification deletion with explicit, revision-bound confirmation."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

from gryphon.errors import ConflictError, SaaSValidationError
from gryphon.models import Channel, SaaSSpec, SpecDeletionChannel, SpecDeletionPreview, Tenant

if TYPE_CHECKING:
    from gryphon.saas_store import SaaSStore

MAX_CONFIRM_NAME = 128
TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")


def _lineage(specs: dict[str, SaaSSpec], spec_id: str) -> tuple[SaaSSpec, list[str]]:
    """Find ancestors then all descendants without name-based merging or unbounded recursion."""
    root = specs[spec_id]
    seen: set[str] = set()
    while root.parent_id is not None:
        if root.id in seen or root.parent_id not in specs:
            raise ConflictError("Specification lineage is invalid")
        seen.add(root.id)
        root = specs[root.parent_id]
    children: dict[str, list[str]] = {}
    for item in specs.values():
        if item.parent_id is not None:
            children.setdefault(item.parent_id, []).append(item.id)
    pending, selected = [root.id], set[str]()
    while pending:
        current = pending.pop()
        if current in selected or specs[current].name != root.name:
            raise ConflictError("Specification lineage is invalid")
        selected.add(current)
        pending.extend(children.get(current, []))
    return root, sorted(selected)


async def _channels(store: SaaSStore, tenant_id: str, version_ids: list[str]) -> list[Channel]:
    """Read all affected channels, including disabled channels and older pinned bindings."""
    rows = await store._db.execute("SELECT id,payload FROM saas_channels WHERE tenant_id=? ORDER BY id", (tenant_id,))
    bindings = await store._db.execute("SELECT channel_id,spec_id FROM saas_bindings WHERE tenant_id=?", (tenant_id,))
    relational: dict[str, set[str]] = {}
    for binding in bindings:
        relational.setdefault(str(binding["channel_id"]), set()).add(str(binding["spec_id"]))
    selected, affected = set(version_ids), list[Channel]()
    for row in rows:
        channel = Channel.model_validate_json(str(row["payload"]))
        saved = relational.get(str(row["id"]), set())
        if selected.intersection(saved | set(channel.spec_ids)):
            if channel.id != row["id"] or channel.tenant_id != tenant_id or saved != set(channel.spec_ids):
                raise ConflictError("Specification bindings are invalid")
            affected.append(channel)
    return affected


async def _preview(store: SaaSStore, tenant_id: str, spec_id: str) -> tuple[SpecDeletionPreview, list[Channel]]:
    """Derive one consistent snapshot inside the caller-owned serialized transaction."""
    await store._get("tenants", Tenant, tenant_id)
    await store._get("specs", SaaSSpec, spec_id, tenant_id)
    rows = await store._db.execute("SELECT id,payload FROM saas_specs WHERE tenant_id=?", (tenant_id,))
    specs: dict[str, SaaSSpec] = {}
    for row in rows:
        item = SaaSSpec.model_validate_json(str(row["payload"]))
        if item.id != row["id"] or item.tenant_id != tenant_id:
            raise ConflictError("Specification lineage is invalid")
        specs[item.id] = item
    root, version_ids = _lineage(specs, spec_id)
    channels = await _channels(store, tenant_id, version_ids)
    preview = SpecDeletionPreview(
        name=root.name,
        specification_id=root.id,
        spec_id=spec_id,
        version_ids=version_ids,
        version_count=len(version_ids),
        channels=[SpecDeletionChannel(id=item.id, name=item.name, revision=item.revision) for item in channels],
        confirmation_token="0" * 64,
    )
    canonical = json.dumps(
        {
            "tenant_id": tenant_id,
            "preview": preview.model_dump(exclude={"confirmation_token"}),
            "channels": [item.model_dump(mode="json") for item in channels],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    preview.confirmation_token = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return preview, channels


async def preview_spec_deletion(store: SaaSStore, tenant_id: str, spec_id: str) -> SpecDeletionPreview:
    """Preview complete lineage impact without requiring an enabled tenant or mutating data."""
    async with store._db.transaction():
        preview, _ = await _preview(store, tenant_id, spec_id)
        return preview


def _confirm(preview: SpecDeletionPreview, confirm_name: object, confirmation_token: object) -> None:
    """Require an exact bounded stored name and a well-formed current snapshot digest."""
    if not isinstance(confirm_name, str) or not 1 <= len(confirm_name) <= MAX_CONFIRM_NAME:
        raise SaaSValidationError("Type the exact specification name to confirm deletion")
    if confirm_name != preview.name:
        raise SaaSValidationError("Type the exact specification name to confirm deletion")
    if not isinstance(confirmation_token, str) or TOKEN_PATTERN.fullmatch(confirmation_token) is None:
        raise SaaSValidationError("Invalid specification deletion confirmation token")
    if confirmation_token != preview.confirmation_token:
        raise ConflictError("Specification deletion impact changed; preview and confirm again")


async def delete_spec(
    store: SaaSStore, tenant_id: str, spec_id: str, confirm_name: object, confirmation_token: object
) -> tuple[SpecDeletionPreview, list[Channel]]:
    """Check confirmation before mutation; commit unbindings, deletion and bounded audits together."""
    async with store._db.transaction():
        preview, channels = await _preview(store, tenant_id, spec_id)
        _confirm(preview, confirm_name, confirmation_token)
        selected = set(preview.version_ids)
        for channel in channels:
            channel.spec_ids = [value for value in channel.spec_ids if value not in selected]
            channel.revision += 1
            await store._bindings(channel)
            await store._db.execute(
                "UPDATE saas_channels SET payload=? WHERE tenant_id=? AND id=?",
                (channel.model_dump_json(), tenant_id, channel.id),
            )
            await store._audit(tenant_id, "channel_updated", channel.id)
        for version_id in preview.version_ids:
            await store._db.execute("DELETE FROM saas_specs WHERE tenant_id=? AND id=?", (tenant_id, version_id))
        await store._audit(tenant_id, "spec_deleted", spec_id=preview.specification_id)
        return preview, channels
