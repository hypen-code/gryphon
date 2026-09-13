"""Immutable hosted specification provenance and bounded compilation diagnostics."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from gryphon.models.mcp import MCPBinding
from gryphon.models.operation_policy import ReadOnlyPostOperation


class SpecDiagnostics(BaseModel):
    """Describe policy filtering without implying that unsupported operations are callable."""

    total_operations: int = Field(default=0, ge=0)
    available_operations: int = Field(default=0, ge=0)
    filtered_operations: int = Field(default=0, ge=0)
    unsupported_operations: int = Field(default=0, ge=0)


class SpecImport(BaseModel):
    """A validated snapshot with separately reviewed, spec-scoped POST read grants.

    Grants are trusted control-plane metadata, never parsed from document hints or
    accepted as ordinary browser import fields. Legacy snapshots default to no grants.
    """

    document: dict[str, Any] = Field(default_factory=dict, repr=False)
    read_only_filter: bool = Field(default=True, strict=True)
    approved_post_reads: list[ReadOnlyPostOperation] = Field(
        default_factory=list[ReadOnlyPostOperation], max_length=1000
    )
    source_type: Literal["file", "openapi_url", "ucp_url"] = "file"
    source_url: str | None = Field(default=None, max_length=2048)
    resolved_profile_url: str | None = Field(default=None, max_length=2048)
    resolved_endpoint: str | None = Field(default=None, max_length=2048)
    source_transport: Literal["rest", "mcp"] | None = None
    mcp_bindings: dict[str, MCPBinding] = Field(default_factory=dict[str, MCPBinding], max_length=1000, repr=False)
    diagnostics: SpecDiagnostics | None = None
    warnings: list[str] = Field(default_factory=list, max_length=100)


class SaaSSpec(SpecImport):
    """Immutable tenant-owned canonical JSON specification."""

    id: str
    tenant_id: str
    name: str = Field(min_length=1, max_length=128)
    sha256: str
    created_at: float
    parent_id: str | None = None


class SpecDeletionChannel(BaseModel):
    """Public impact summary for a channel whose bindings will change."""

    id: str
    name: str
    revision: int


class SpecDeletionPreview(BaseModel):
    """Whole-lineage deletion impact and a non-secret compare-and-swap digest."""

    name: str
    specification_id: str
    spec_id: str
    version_ids: list[str]
    version_count: int
    channels: list[SpecDeletionChannel]
    confirmation_token: str = Field(pattern=r"^[0-9a-f]{64}$")
