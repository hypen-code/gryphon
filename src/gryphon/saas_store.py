"""Tenant-scoped SaaS configuration, hashed channel keys, and safe aggregate telemetry."""

from __future__ import annotations

import hashlib
import math
import secrets
import time
from typing import Any, Literal
from uuid import uuid4

from gryphon.errors import SaaSDisabledError, SaaSValidationError
from gryphon.models import (
    Channel,
    ChannelUsage,
    SaaSSpec,
    SpecDeletionPreview,
    SpecImport,
    Tenant,
    UserAccount,
)
from gryphon.saas_audit_archive import MAX_AUDIT_ENTRIES
from gryphon.saas_database import SaaSDatabase, SQLValue
from gryphon.saas_records import SaaSRecords
from gryphon.saas_resource_delete import (
    delete_channel,
    delete_tenant,
    preview_channel_deletion,
    preview_tenant_deletion,
)
from gryphon.saas_spec_delete import delete_spec, preview_spec_deletion
from gryphon.saas_spec_versions import insert_spec, new_spec, refresh_spec

TOOLS = frozenset(
    {
        "list_servers",
        "search_functions",
        "get_functions",
        "execute_code",
        "run_cached_code",
        "submit_code",
        "get_run",
        "cancel_run",
        "list_recipes",
        "read_artifact",
        "transform_artifact",
        "list_skills",
        "get_server_skills",
    }
)
MAX_IMPORT_LENGTH = 128
MAX_TOKEN_LENGTH = 256
MAX_LATENCY_MS = 86400000


class SaaSStore(SaaSRecords):
    """Persist bounded administrative resources without storing plaintext access keys."""

    def __init__(
        self,
        database_url: str,
        *,
        max_tenants: int = 1000,
        max_channels_per_tenant: int = 100,
        max_specs_per_tenant: int = 100,
        max_spec_bytes: int = 2097152,
        max_list_limit: int = 100,
        max_audit_entries: int = 10000,
    ) -> None:
        """Configure explicit storage and positive quotas, without environment access."""
        limits = (
            max_tenants,
            max_channels_per_tenant,
            max_specs_per_tenant,
            max_spec_bytes,
            max_list_limit,
            max_audit_entries,
        )
        if any(value < 1 for value in limits):
            raise SaaSValidationError("Storage limits must be positive")
        if max_audit_entries > MAX_AUDIT_ENTRIES:
            raise SaaSValidationError("Audit retention exceeds storage limit")
        self._db = SaaSDatabase(database_url)
        self._max_tenants, self._max_channels, self._max_specs = limits[:3]
        self._max_spec_bytes, self._max_list, self._max_audit = limits[3:]

    async def initialize(self) -> None:
        """Open the store and install its relational schema."""
        await self._db.initialize()

    async def close(self) -> None:
        """Close the connection after in-flight transactions finish."""
        await self._db.close()

    async def acquire_host_lease(self) -> None:
        """Require exclusive hosted-worker ownership after initialization and before listening."""
        await self._db.acquire_host_lease()

    async def _tenant(self, tenant_id: str) -> Tenant:
        """Require an enabled tenant before mutating child configuration."""
        tenant = await self._get("tenants", Tenant, tenant_id)
        if not tenant.enabled:
            raise SaaSDisabledError("Tenant is disabled")
        return tenant

    async def create_tenant(self, name: str) -> Tenant:
        """Create a server-generated tenant identity within the retained-tenant quota."""
        item = Tenant(id=str(uuid4()), name=name, created_at=time.time())
        async with self._db.transaction():
            await self._quota("tenants", self._max_tenants)
            await self._db.execute("INSERT INTO saas_tenants VALUES (?,?,?)", (item.id, 1, item.model_dump_json()))
            await self._audit(item.id, "tenant_created")
        return item

    async def get_tenant(self, tenant_id: str) -> Tenant:
        """Get administrative tenant metadata, including disabled records."""
        async with self._db.transaction():
            return await self._get("tenants", Tenant, tenant_id)

    async def list_tenants(self, limit: int = 100, offset: int = 0) -> list[Tenant]:
        """List retained tenants in a bounded stable page."""
        async with self._db.transaction():
            return await self._list("tenants", Tenant, None, limit, offset)

    async def disable_tenant(self, tenant_id: str) -> Tenant:
        """Disable all authentication for a tenant without deleting its resources."""
        return await self.set_tenant_enabled(tenant_id, False)

    async def set_tenant_enabled(self, tenant_id: str, enabled: bool) -> Tenant:
        """Set tenant authority and advance every owned channel revision in one transaction."""
        async with self._db.transaction():
            item = await self._get("tenants", Tenant, tenant_id)
            item.enabled = enabled
            await self._db.execute(
                "UPDATE saas_tenants SET enabled=?,payload=? WHERE id=?",
                (int(enabled), item.model_dump_json(), tenant_id),
            )
            rows = await self._db.execute("SELECT payload FROM saas_channels WHERE tenant_id=?", (tenant_id,))
            for row in rows:
                channel = Channel.model_validate_json(str(row["payload"]))
                channel.revision += 1
                await self._db.execute(
                    "UPDATE saas_channels SET payload=? WHERE tenant_id=? AND id=?",
                    (channel.model_dump_json(), tenant_id, channel.id),
                )
            await self._db.execute("UPDATE saas_users SET revision=revision+1 WHERE tenant_id=?", (tenant_id,))
            await self._audit(tenant_id, "tenant_enabled" if enabled else "tenant_disabled")
            return item

    async def create_spec(
        self, tenant_id: str, name: str, document: dict[str, Any], *, imported: SpecImport | None = None
    ) -> SaaSSpec:
        """Store canonical bounded JSON; API parsing and OpenAPI validation precede this call."""
        item = new_spec(tenant_id, name, document, self._max_spec_bytes, imported)
        async with self._db.transaction():
            await self._tenant(tenant_id)
            await insert_spec(self, item)
        return item

    async def refresh_spec(
        self, tenant_id: str, spec_id: str, imported: SpecImport, *, update_channels: bool = False
    ) -> tuple[SaaSSpec, list[Channel]]:
        """Create an immutable successor and optionally advance exact channel bindings atomically."""
        return await refresh_spec(self, tenant_id, spec_id, imported, update_channels=update_channels)

    async def preview_spec_deletion(self, tenant_id: str, spec_id: str) -> SpecDeletionPreview:
        """Preview deletion of all retained versions and affected channel bindings."""
        return await preview_spec_deletion(self, tenant_id, spec_id)

    async def delete_spec(
        self, tenant_id: str, spec_id: str, confirm_name: object, confirmation_token: object
    ) -> tuple[SpecDeletionPreview, list[Channel]]:
        """Delete a confirmed lineage atomically, returning channels requiring runtime cleanup."""
        return await delete_spec(self, tenant_id, spec_id, confirm_name, confirmation_token)

    async def get_spec(self, tenant_id: str, spec_id: str) -> SaaSSpec:
        """Load an immutable specification only from its owning tenant."""
        async with self._db.transaction():
            return await self._get("specs", SaaSSpec, spec_id, tenant_id)

    async def list_specs(self, tenant_id: str, limit: int = 100, offset: int = 0) -> list[SaaSSpec]:
        """List a bounded page of a tenant's immutable specifications."""
        async with self._db.transaction():
            return await self._list("specs", SaaSSpec, tenant_id, limit, offset)

    async def _bindings(self, item: Channel) -> None:
        """Replace bindings atomically, relying on composite foreign keys for isolation."""
        if len(set(item.spec_ids)) != len(item.spec_ids):
            raise SaaSValidationError("Duplicate specification binding")
        for spec_id in item.spec_ids:
            await self._get("specs", SaaSSpec, spec_id, item.tenant_id)
        if any(
            not all(segment.isidentifier() for segment in part.split(".")) or len(part) > MAX_IMPORT_LENGTH
            for part in item.allowed_imports
        ):
            raise SaaSValidationError("Invalid import name")
        await self._db.execute(
            "DELETE FROM saas_bindings WHERE tenant_id=? AND channel_id=?", (item.tenant_id, item.id)
        )
        for spec_id in item.spec_ids:
            await self._db.execute("INSERT INTO saas_bindings VALUES (?,?,?)", (item.tenant_id, item.id, spec_id))

    async def create_channel(
        self,
        tenant_id: str,
        name: str,
        *,
        spec_ids: list[str] | None = None,
        sandbox_mode: Literal["restricted", "docker"] = "restricted",
        allowed_imports: list[str] | None = None,
        include_function_summaries: bool = False,
    ) -> Channel:
        """Create a keyless channel; its random UUID is the execution owner namespace."""
        item = Channel(
            id=str(uuid4()),
            tenant_id=tenant_id,
            name=name,
            spec_ids=spec_ids or [],
            sandbox_mode=sandbox_mode,
            allowed_imports=allowed_imports or [],
            include_function_summaries=include_function_summaries,
            created_at=time.time(),
        )
        async with self._db.transaction():
            await self._tenant(tenant_id)
            await self._quota("channels", self._max_channels, tenant_id)
            await self._db.execute(
                "INSERT INTO saas_channels VALUES (?,?,?,?,?)", (tenant_id, item.id, 1, None, item.model_dump_json())
            )
            await self._bindings(item)
            await self._audit(tenant_id, "channel_created", item.id)
        return item

    async def get_channel(self, tenant_id: str, channel_id: str) -> Channel:
        """Return channel metadata, never its key digest or plaintext key."""
        async with self._db.transaction():
            return await self._get("channels", Channel, channel_id, tenant_id)

    async def list_channels(self, tenant_id: str, limit: int = 100, offset: int = 0) -> list[Channel]:
        """List only the requested tenant's channel configurations."""
        async with self._db.transaction():
            return await self._list("channels", Channel, tenant_id, limit, offset)

    async def update_channel(
        self,
        tenant_id: str,
        channel_id: str,
        *,
        name: str | None = None,
        spec_ids: list[str] | None = None,
        sandbox_mode: Literal["restricted", "docker"] | None = None,
        allowed_imports: list[str] | None = None,
        enabled: bool | None = None,
        include_function_summaries: bool | None = None,
    ) -> Channel:
        """Replace supplied configuration fields and increment revision transactionally."""
        async with self._db.transaction():
            await self._tenant(tenant_id)
            item = await self._get("channels", Channel, channel_id, tenant_id)
            values = item.model_dump()
            values.update(
                {
                    key: value
                    for key, value in {
                        "name": name,
                        "spec_ids": spec_ids,
                        "sandbox_mode": sandbox_mode,
                        "allowed_imports": allowed_imports,
                        "enabled": enabled,
                        "include_function_summaries": include_function_summaries,
                    }.items()
                    if value is not None
                }
            )
            item = Channel.model_validate(values)
            item.revision += 1
            await self._bindings(item)
            await self._db.execute(
                "UPDATE saas_channels SET enabled=?,payload=? WHERE tenant_id=? AND id=?",
                (int(item.enabled), item.model_dump_json(), tenant_id, channel_id),
            )
            await self._audit(tenant_id, "channel_updated", channel_id)
            return item

    async def _key(self, tenant_id: str, channel_id: str, token: str | None) -> None:
        """Change the single active key digest and public key-presence metadata together."""
        async with self._db.transaction():
            if token is not None:
                await self._tenant(tenant_id)
            item = await self._get("channels", Channel, channel_id, tenant_id)
            item.key_active = token is not None
            item.revision += 1
            digest = hashlib.sha256(token.encode()).hexdigest() if token is not None else None
            await self._db.execute(
                "UPDATE saas_channels SET key_digest=?,payload=? WHERE tenant_id=? AND id=?",
                (digest, item.model_dump_json(), tenant_id, channel_id),
            )
            await self._audit(tenant_id, "key_rotated" if token is not None else "key_revoked", channel_id)

    async def rotate_key(self, tenant_id: str, channel_id: str) -> str:
        """Return a fresh high-entropy key once; atomically invalidate the previous key."""
        token = secrets.token_urlsafe(48)
        await self._key(tenant_id, channel_id, token)
        return token

    async def revoke_key(self, tenant_id: str, channel_id: str) -> None:
        """Remove authentication even for a disabled tenant; retain all owned resources."""
        await self._key(tenant_id, channel_id, None)

    async def lookup_key(self, token: str) -> tuple[Tenant, Channel] | None:
        """Hash a presented key and return only enabled tenant/channel authority."""
        if not 32 <= len(token) <= MAX_TOKEN_LENGTH:
            return None
        return await self.lookup_key_digest(hashlib.sha256(token.encode()).hexdigest())

    async def lookup_key_digest(self, digest: str) -> tuple[Tenant, Channel] | None:
        """Resolve an already SHA256-hashed key with one atomic enabled-state snapshot."""
        async with self._db.transaction():
            rows = await self._db.execute(
                "SELECT t.payload AS tenant,c.payload AS channel FROM saas_channels c "
                "JOIN saas_tenants t ON t.id=c.tenant_id WHERE c.key_digest=? AND c.enabled=1 AND t.enabled=1",
                (digest,),
            )
            if not rows:
                return None
            return Tenant.model_validate_json(str(rows[0]["tenant"])), Channel.model_validate_json(
                str(rows[0]["channel"])
            )

    async def record_usage(
        self,
        tenant_id: str,
        channel_id: str,
        tool: str,
        status: Literal["success", "error"],
        latency_ms: float,
    ) -> None:
        """Aggregate finite bounded latency for allowlisted tools; never accept request metadata."""
        if tool not in TOOLS or status not in {"success", "error"} or not math.isfinite(latency_ms):
            raise SaaSValidationError("Invalid usage dimensions")
        if not 0 <= latency_ms <= MAX_LATENCY_MS:
            raise SaaSValidationError("Invalid usage latency")
        async with self._db.transaction():
            await self._get("channels", Channel, channel_id, tenant_id)
            await self._db.execute(
                "INSERT INTO saas_usage VALUES (?,?,?,?,1,?) ON CONFLICT(tenant_id,channel_id,tool,status) "
                "DO UPDATE SET calls=saas_usage.calls+1,latency_ms=saas_usage.latency_ms+excluded.latency_ms",
                (tenant_id, channel_id, tool, status, latency_ms),
            )

    async def list_usage(
        self,
        tenant_id: str,
        channel_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ChannelUsage]:
        """Return bounded aggregate counters scoped to a tenant and optional channel."""
        self._page(limit, offset)
        suffix = "" if channel_id is None else " AND channel_id=?"
        params: tuple[SQLValue, ...] = (tenant_id,) if channel_id is None else (tenant_id, channel_id)
        async with self._db.transaction():
            rows = await self._db.execute(
                "SELECT channel_id,tool,status,calls,latency_ms FROM saas_usage WHERE tenant_id=?"
                f"{suffix} ORDER BY channel_id,tool,status LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            return [ChannelUsage.model_validate(row) for row in rows]

    async def preview_channel_deletion(self, tenant_id: str, channel_id: str) -> dict[str, object]:
        """Preview exact channel impact and a configuration-bound confirmation digest."""
        return await preview_channel_deletion(self, tenant_id, channel_id)

    async def delete_channel(
        self, tenant_id: str, channel_id: str, confirm_name: object, confirmation_token: object
    ) -> Channel:
        """Delete confirmed channel rows atomically; caller drains its runtime after commit."""
        return await delete_channel(self, tenant_id, channel_id, confirm_name, confirmation_token)

    async def preview_tenant_deletion(self, tenant_id: str) -> dict[str, object]:
        """Preview the complete workspace, including disabled accounts and all spec versions."""
        return await preview_tenant_deletion(self, tenant_id)

    async def delete_tenant(
        self, tenant_id: str, confirm_name: object, confirmation_token: object, actor_id: str
    ) -> tuple[Tenant, list[Channel], list[UserAccount]]:
        """Delete a confirmed workspace; caller revokes sessions and drains returned runtimes."""
        return await delete_tenant(self, tenant_id, confirm_name, confirmation_token, actor_id)
