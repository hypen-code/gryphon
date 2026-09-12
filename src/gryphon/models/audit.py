"""Safe administrative actors and static resource events, without request content."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AuditEvent = Literal[
    "tenant_created",
    "tenant_disabled",
    "tenant_enabled",
    "spec_created",
    "channel_created",
    "channel_updated",
    "key_rotated",
    "key_revoked",
    "post_reads_updated",
]


class AuditActor(BaseModel):
    """Public attribution only; display_source distinguishes snapshots from current names.

    Missing legacy attribution is unknown, never evidence of bootstrap authority.
    Actor IDs are durable even when an account's display name changes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str | None = None
    username: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=128)
    kind: Literal["user", "bootstrap", "system", "unknown"] = "unknown"
    display_source: Literal["snapshot", "current", "unknown"] = "unknown"


class AdminAudit(BaseModel):
    """Static administrative event with an optional historical public actor snapshot."""

    id: str
    tenant_id: str
    channel_id: str | None = None
    event: AuditEvent
    created_at: float
    actor: AuditActor = Field(default_factory=AuditActor)
