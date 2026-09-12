"""Atomic immutable specification versions and opt-in replacement of exact channel bindings."""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from gryphon.errors import ConflictError, SaaSQuotaError, SaaSValidationError
from gryphon.models import Channel, SaaSSpec, SpecImport

if TYPE_CHECKING:
    from gryphon.saas_store import SaaSStore


def new_spec(
    tenant_id: str, name: str, document: dict[str, Any], limit: int, imported: SpecImport | None = None
) -> SaaSSpec:
    """Canonicalize the stored snapshot without trusting import metadata as execution authority."""
    try:
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        encoded = canonical.encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise SaaSValidationError("Specification must be canonical JSON") from exc
    if len(encoded) > limit:
        raise SaaSQuotaError("Specification byte quota exceeded")
    metadata = imported.model_dump(exclude={"document"}) if imported is not None else {}
    return SaaSSpec(
        id=str(uuid4()),
        tenant_id=tenant_id,
        name=name,
        document=json.loads(canonical),
        sha256=hashlib.sha256(encoded).hexdigest(),
        created_at=time.time(),
        **metadata,
    )


async def insert_spec(store: SaaSStore, item: SaaSSpec) -> None:
    """Insert and audit inside the caller-owned transaction, applying retained-version quotas."""
    await store._quota("specs", store._max_specs, item.tenant_id)
    await store._db.execute("INSERT INTO saas_specs VALUES (?,?,?)", (item.tenant_id, item.id, item.model_dump_json()))
    await store._audit(item.tenant_id, "spec_created")


async def refresh_spec(
    store: SaaSStore, tenant_id: str, spec_id: str, imported: SpecImport, *, update_channels: bool
) -> tuple[SaaSSpec, list[Channel]]:
    """Commit version, revisions, bindings and audit together, rejecting stale concurrent refreshes."""
    async with store._db.transaction():
        await store._tenant(tenant_id)
        previous = await store._get("specs", SaaSSpec, spec_id, tenant_id)
        if previous.source_type != imported.source_type or previous.source_url != imported.source_url:
            raise ConflictError("Specification provenance cannot change during refresh")
        rows = await store._db.execute("SELECT payload FROM saas_specs WHERE tenant_id=?", (tenant_id,))
        if any(SaaSSpec.model_validate_json(str(row["payload"])).parent_id == spec_id for row in rows):
            raise ConflictError("Refresh the latest specification version")
        item = new_spec(tenant_id, previous.name, imported.document, store._max_spec_bytes, imported)
        if (item.sha256, item.diagnostics, item.warnings) == (previous.sha256, previous.diagnostics, previous.warnings):
            return previous, []
        item.parent_id = previous.id
        await insert_spec(store, item)
        channels = await replace_bindings(store, item) if update_channels else []
        return item, channels


async def replace_bindings(store: SaaSStore, item: SaaSSpec) -> list[Channel]:
    """Advance only channels bound to this exact parent while preserving all other configuration."""
    rows = await store._db.execute(
        "SELECT c.payload FROM saas_channels c JOIN saas_bindings b "
        "ON c.tenant_id=b.tenant_id AND c.id=b.channel_id WHERE b.tenant_id=? AND b.spec_id=? ORDER BY c.id",
        (item.tenant_id, item.parent_id),
    )
    channels = []
    for row in rows:
        channel = Channel.model_validate_json(str(row["payload"]))
        channel.spec_ids = [item.id if value == item.parent_id else value for value in channel.spec_ids]
        channel.revision += 1
        await store._bindings(channel)
        await store._db.execute(
            "UPDATE saas_channels SET payload=? WHERE tenant_id=? AND id=?",
            (channel.model_dump_json(), item.tenant_id, channel.id),
        )
        await store._audit(item.tenant_id, "channel_updated", channel.id)
        channels.append(channel)
    return channels
