"""Password-free hosted account identities and static administrative audits."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gryphon.models.audit import AuditActor

UserRole = Literal["platform_admin", "tenant_user"]
UserAuditEvent = Literal[
    "user_created", "user_updated", "user_disabled", "user_enabled", "password_reset", "password_changed"
]
USERNAME_PATTERN = re.compile(r"[a-z0-9._@+-]{3,128}", re.ASCII)


def normalize_username(value: str) -> str:
    """Normalize ASCII case without accepting whitespace or Unicode lookalikes."""
    if not 3 <= len(value) <= 128 or not value.isascii() or USERNAME_PATTERN.fullmatch(value.lower()) is None:
        raise ValueError("Invalid username")
    return value.lower()


class UserAccount(BaseModel):
    """Public immutable-role identity containing no password or password hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    username: str = Field(min_length=3, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    role: UserRole
    tenant_id: str | None = None
    enabled: bool = True
    revision: int = Field(default=1, ge=1)
    created_at: float = Field(ge=0, allow_inf_nan=False)

    @field_validator("id", "tenant_id")
    @classmethod
    def _identifier(cls, value: str | None) -> str | None:
        """Require canonical server UUID identifiers when present."""
        if value is not None and str(UUID(value)) != value:
            raise ValueError("Invalid account identifier")
        return value

    @field_validator("username")
    @classmethod
    def _username(cls, value: str) -> str:
        """Apply the shared ASCII username policy."""
        return normalize_username(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        """Reject control, formatting, surrogate, and other non-display characters."""
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("Invalid display name")
        return value

    @model_validator(mode="after")
    def _membership(self) -> UserAccount:
        """Bind tenant users to exactly one tenant and platform admins to none."""
        if (self.role == "tenant_user") != (self.tenant_id is not None):
            raise ValueError("Invalid account membership")
        return self


class UserAudit(BaseModel):
    """Static account events with public actor display metadata, never login inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    actor_id: str
    subject_id: str
    tenant_id: str | None
    event: UserAuditEvent
    created_at: float
    actor: AuditActor = Field(default_factory=AuditActor)
