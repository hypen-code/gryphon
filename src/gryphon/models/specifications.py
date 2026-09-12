"""Immutable hosted specification provenance and bounded compilation diagnostics."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SpecDiagnostics(BaseModel):
    """Describe policy filtering without implying that unsupported operations are callable."""

    total_operations: int = Field(default=0, ge=0)
    available_operations: int = Field(default=0, ge=0)
    filtered_operations: int = Field(default=0, ge=0)
    unsupported_operations: int = Field(default=0, ge=0)


class SpecImport(BaseModel):
    """A validated snapshot and provenance, not authority for remote execution."""

    document: dict[str, Any] = Field(default_factory=dict, repr=False)
    source_type: Literal["file", "openapi_url", "ucp_url"] = "file"
    source_url: str | None = Field(default=None, max_length=2048)
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
