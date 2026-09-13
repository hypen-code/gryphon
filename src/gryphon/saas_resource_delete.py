"""Confirmed physical workspace deletion without touching private runtime files."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

from gryphon.errors import ConflictError, SaaSValidationError
from gryphon.models import Channel, Tenant, UserAccount
from gryphon.saas_audit_archive import append_user_deletion, archive_resource_audit, archive_user_audit, prune_audit
from gryphon.saas_users import MAX_AUDIT_ENTRIES, PUBLIC_COLUMNS, _actor

if TYPE_CHECKING:
    from gryphon.saas_store import SaaSStore

MAX_CONFIRM_NAME = 128
TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")
CHANNEL_DEPENDENCIES = (
    "bindings",
    "usage",
    "analytics_daily",
    "analytics_receipts",
    "analytics_metadata",
)


def _digest(value: object) -> str:
    """Hash only public configuration snapshots, excluding traffic and credential material."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _confirm(name: str, token: object, confirm_name: object, confirmation_token: object) -> None:
    """Check exact typed name and current CAS before any mutation, never treating CAS as authority."""
    if not isinstance(confirm_name, str) or not 1 <= len(confirm_name) <= MAX_CONFIRM_NAME:
        raise SaaSValidationError("Type the exact resource name to confirm deletion")
    if not isinstance(confirmation_token, str) or TOKEN_PATTERN.fullmatch(confirmation_token) is None:
        raise SaaSValidationError("Invalid resource deletion confirmation token")
    if confirmation_token != token:
        raise ConflictError("Resource deletion impact changed; preview and confirm again")
    if confirm_name != name:
        raise SaaSValidationError("Type the exact resource name to confirm deletion")


async def _bindings(store: SaaSStore, tenant_id: str, channel_id: str | None = None) -> list[dict[str, object]]:
    """Read relational bindings in stable order without loading private channel key digests."""
    suffix, params = ("", (tenant_id,)) if channel_id is None else (" AND channel_id=?", (tenant_id, channel_id))
    rows = await store._db.execute(
        "SELECT channel_id,spec_id FROM saas_bindings WHERE tenant_id=?" + suffix + " ORDER BY channel_id,spec_id",
        params,
    )
    return [{"channel_id": row["channel_id"], "spec_id": row["spec_id"]} for row in rows]


async def _channel_preview(store: SaaSStore, tenant_id: str, channel_id: str) -> tuple[dict[str, object], Channel]:
    """Snapshot even disabled channels under the caller's serialized transaction."""
    await store._get("tenants", Tenant, tenant_id)
    channel = await store._get("channels", Channel, channel_id, tenant_id)
    if channel.id != channel_id or channel.tenant_id != tenant_id:
        raise ConflictError("Channel identity is invalid")
    token = _digest(
        {
            "kind": "channel",
            "tenant_id": tenant_id,
            "channel": channel.model_dump(mode="json"),
            "bindings": await _bindings(store, tenant_id, channel_id),
        }
    )
    return {
        "kind": "channel",
        "id": channel.id,
        "name": channel.name,
        "label": channel.name,
        "confirmation_token": token,
        "impact": {"channels": 1, "users": 0, "specs": 0},
    }, channel


async def preview_channel_deletion(store: SaaSStore, tenant_id: str, channel_id: str) -> dict[str, object]:
    """Preview a channel without altering state or requiring it to be enabled."""
    async with store._db.transaction():
        preview, _ = await _channel_preview(store, tenant_id, channel_id)
        return preview


async def _remove_channels(store: SaaSStore, tenant_id: str, channel_id: str | None = None) -> None:
    """Remove dependent rows before channel rows with exact tenant scope and foreign keys enabled."""
    suffix, params = ("", (tenant_id,)) if channel_id is None else (" AND channel_id=?", (tenant_id, channel_id))
    for table in CHANNEL_DEPENDENCIES:
        await store._db.execute(f"DELETE FROM saas_{table} WHERE tenant_id=?{suffix}", params)
    channel_suffix = "" if channel_id is None else " AND id=?"
    await store._db.execute(f"DELETE FROM saas_channels WHERE tenant_id=?{channel_suffix}", params)


async def delete_channel(
    store: SaaSStore, tenant_id: str, channel_id: str, confirm_name: object, confirmation_token: object
) -> Channel:
    """Commit audit and physical channel deletion together; runtime cleanup belongs to the caller."""
    async with store._db.transaction():
        preview, channel = await _channel_preview(store, tenant_id, channel_id)
        _confirm(channel.name, preview["confirmation_token"], confirm_name, confirmation_token)
        await store._audit(tenant_id, "channel_deleted", channel_id, resource_name=channel.name)
        await _remove_channels(store, tenant_id, channel_id)
        return channel


async def _workspace_channels(store: SaaSStore, tenant_id: str) -> list[Channel]:
    """Read every public channel configuration, not merely one administrative listing page."""
    rows = await store._db.execute("SELECT id,payload FROM saas_channels WHERE tenant_id=? ORDER BY id", (tenant_id,))
    channels = []
    for row in rows:
        channel = Channel.model_validate_json(str(row["payload"]))
        if channel.id != row["id"] or channel.tenant_id != tenant_id:
            raise ConflictError("Channel identity is invalid")
        channels.append(channel)
    return channels


async def _workspace_users(store: SaaSStore, tenant_id: str) -> list[UserAccount]:
    """Read public revisioned members only; platform accounts can never be workspace children."""
    rows = await store._db.execute(
        f"SELECT {PUBLIC_COLUMNS} FROM saas_users WHERE tenant_id=? ORDER BY id", (tenant_id,)
    )
    users = []
    for row in rows:
        if row["role"] != "tenant_user" or row["tenant_id"] != tenant_id:
            raise ConflictError("Tenant membership is invalid")
        users.append(UserAccount.model_validate(row))
    return users


async def _tenant_preview(
    store: SaaSStore, tenant_id: str
) -> tuple[dict[str, object], Tenant, list[Channel], list[UserAccount]]:
    """Snapshot complete public workspace state, with immutable spec IDs rather than document bodies."""
    tenant = await store._get("tenants", Tenant, tenant_id)
    if tenant.id != tenant_id:
        raise ConflictError("Tenant identity is invalid")
    channels = await _workspace_channels(store, tenant_id)
    users = await _workspace_users(store, tenant_id)
    specs = await store._db.execute("SELECT id FROM saas_specs WHERE tenant_id=? ORDER BY id", (tenant_id,))
    spec_ids = [str(row["id"]) for row in specs]
    token = _digest(
        {
            "kind": "tenant",
            "tenant": tenant.model_dump(mode="json"),
            "channels": [item.model_dump(mode="json") for item in channels],
            "users": [item.model_dump(mode="json") for item in users],
            "spec_ids": spec_ids,
            "bindings": await _bindings(store, tenant_id),
        }
    )
    preview: dict[str, object] = {
        "kind": "tenant",
        "id": tenant.id,
        "name": tenant.name,
        "label": tenant.name,
        "confirmation_token": token,
        "impact": {"users": len(users), "channels": len(channels), "specs": len(spec_ids)},
        "user_ids": [item.id for item in users],
        "channel_ids": [item.id for item in channels],
        "spec_ids": spec_ids,
    }
    return preview, tenant, channels, users


async def preview_tenant_deletion(store: SaaSStore, tenant_id: str) -> dict[str, object]:
    """Preview all assigned users/channels/spec versions, including disabled resources."""
    async with store._db.transaction():
        preview, _, _, _ = await _tenant_preview(store, tenant_id)
        return preview


async def delete_tenant(
    store: SaaSStore, tenant_id: str, confirm_name: object, confirmation_token: object, actor_id: str
) -> tuple[Tenant, list[Channel], list[UserAccount]]:
    """Delete all workspace rows atomically, retaining only normally bounded public audit history."""
    if not isinstance(actor_id, str):
        raise SaaSValidationError("Invalid audit actor")
    _actor(actor_id)
    async with store._db.transaction():
        preview, tenant, channels, users = await _tenant_preview(store, tenant_id)
        _confirm(tenant.name, preview["confirmation_token"], confirm_name, confirmation_token)
        await archive_user_audit(store._db, [item.id for item in users])
        for user in users:
            await append_user_deletion(store._db, user, actor_id)
        await prune_audit(store._db, "user", MAX_AUDIT_ENTRIES)
        await store._db.execute("DELETE FROM saas_users WHERE tenant_id=?", (tenant_id,))
        await _remove_channels(store, tenant_id)
        await store._db.execute("DELETE FROM saas_specs WHERE tenant_id=?", (tenant_id,))
        await store._audit(tenant_id, "tenant_deleted", resource_name=tenant.name)
        await archive_resource_audit(store._db, tenant_id)
        await store._db.execute("DELETE FROM saas_tenants WHERE id=?", (tenant_id,))
        return tenant, channels, users
