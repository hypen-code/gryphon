"""Typed internal record helpers shared by transactional control-plane operations."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel

from gryphon.errors import SaaSNotFoundError, SaaSQuotaError
from gryphon.models import AdminAudit, AuditEvent, Channel, Tenant
from gryphon.saas_audit import current_actor
from gryphon.saas_audit_archive import prune_audit

if TYPE_CHECKING:
    from gryphon.saas_database import SaaSDatabase, SQLValue

Table = Literal["tenants", "specs", "channels"]


class SaaSRecords:
    """Provide scoped SQL helpers under transactions owned by the concrete store."""

    _db: SaaSDatabase
    _max_list: int
    _max_audit: int

    async def _get[T: BaseModel](self, table: Table, model: type[T], id: str, tenant_id: str | None = None) -> T:
        """Load one resource using mandatory tenant scope for child resources."""
        suffix, params = ("", (id,)) if table == "tenants" else (" AND tenant_id=?", (id, tenant_id))
        rows = await self._db.execute(f"SELECT payload FROM saas_{table} WHERE id=?{suffix}", params)
        if not rows:
            raise SaaSNotFoundError("Control-plane resource not found")
        return model.model_validate_json(str(rows[0]["payload"]))

    async def is_current_channel(self, channel: Channel) -> bool:
        """Validate exact enabled runtime authority without retrieving key digests or granting key access."""
        async with self._db.transaction():
            rows = await self._db.execute(
                "SELECT t.payload AS tenant,c.payload AS channel FROM saas_channels c "
                "JOIN saas_tenants t ON t.id=c.tenant_id "
                "WHERE c.id=? AND c.tenant_id=? AND c.enabled=1 AND t.enabled=1",
                (channel.id, channel.tenant_id),
            )
            if not rows:
                return False
            tenant = Tenant.model_validate_json(str(rows[0]["tenant"]))
            current = Channel.model_validate_json(str(rows[0]["channel"]))
            return tenant.id == channel.tenant_id and tenant.enabled and current.enabled and current == channel

    async def _quota(self, table: Table, maximum: int, tenant_id: str | None = None) -> None:
        """Count retained resources while holding the cross-process transaction lock."""
        suffix, params = ("", ()) if tenant_id is None else (" WHERE tenant_id=?", (tenant_id,))
        rows = await self._db.execute(f"SELECT COUNT(*) AS count FROM saas_{table}{suffix}", params)
        if int(str(rows[0]["count"])) >= maximum:
            raise SaaSQuotaError("Control-plane resource quota exceeded")

    def _page(self, limit: int, offset: int) -> None:
        """Reject unbounded or negative administrative listing requests."""
        if not 1 <= limit <= self._max_list or not 0 <= offset <= self._max_audit:
            raise SaaSQuotaError("Control-plane listing quota exceeded")

    async def _list[T: BaseModel](
        self,
        table: Table,
        model: type[T],
        tenant_id: str | None,
        limit: int,
        offset: int,
    ) -> list[T]:
        """Read a deterministic bounded page without widening tenant scope."""
        self._page(limit, offset)
        suffix = "" if tenant_id is None else " WHERE tenant_id=?"
        params: tuple[SQLValue, ...] = () if tenant_id is None else (tenant_id,)
        rows = await self._db.execute(
            f"SELECT payload FROM saas_{table}{suffix} ORDER BY id LIMIT ? OFFSET ?", (*params, limit, offset)
        )
        return [model.model_validate_json(str(row["payload"])) for row in rows]

    async def _audit(
        self,
        tenant_id: str,
        event: AuditEvent,
        channel_id: str | None = None,
        *,
        spec_id: str | None = None,
        resource_name: str | None = None,
    ) -> None:
        """Append public attribution and prune combined live/archive history in the caller transaction."""
        item = AdminAudit(
            id=str(uuid4()),
            tenant_id=tenant_id,
            channel_id=channel_id,
            spec_id=spec_id,
            resource_name=resource_name,
            event=event,
            created_at=time.time(),
            actor=current_actor(),
        )
        await self._db.execute(
            "INSERT INTO saas_audit VALUES (?,?,?,?)", (item.id, tenant_id, item.created_at, item.model_dump_json())
        )
        await prune_audit(self._db, "resource", self._max_audit)

    async def list_audit(self, tenant_id: str, limit: int = 100, offset: int = 0) -> list[AdminAudit]:
        """Merge retained live and archived resource events for exactly one tenant, newest first."""
        self._page(limit, offset)
        async with self._db.transaction():
            rows = await self._db.execute(
                "SELECT payload FROM (SELECT id,created_at,payload FROM saas_audit WHERE tenant_id=? "
                "UNION ALL SELECT id,created_at,payload FROM saas_audit_archive "
                "WHERE category='resource' AND tenant_id=?) AS events "
                "ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (tenant_id, tenant_id, limit, offset),
            )
            return [AdminAudit.model_validate_json(str(row["payload"])) for row in rows]

    async def list_deleted_tenant_audit(self, limit: int = 100, offset: int = 0) -> list[AdminAudit]:
        """Return deleted-tenant resource history only; callers must require platform authority."""
        self._page(limit, offset)
        async with self._db.transaction():
            rows = await self._db.execute(
                "SELECT a.payload FROM saas_audit_archive a WHERE a.category='resource' "
                "AND NOT EXISTS (SELECT 1 FROM saas_tenants t WHERE t.id=a.tenant_id) "
                "ORDER BY a.created_at DESC,a.id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            return [AdminAudit.model_validate_json(str(row["payload"])) for row in rows]
