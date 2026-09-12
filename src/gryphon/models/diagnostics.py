"""Closed, content-free execution diagnostics and Gryphon-owned failure messages."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_DIAGNOSTIC_LINE = 1_000_000
MAX_UPSTREAM_CODE_LENGTH = 64
type DiagnosticPhase = Literal["discovery", "invoke"]
type UpstreamCode = Literal["invalid_profile_url"]
type ASTViolationType = Literal[
    "blocked_import",
    "blocked_call",
    "blocked_attribute_call",
    "blocked_attribute",
    "blocked_global",
    "blocked_nonlocal",
]
UCP_PROFILE_MESSAGE = (
    "UCP agent profile is missing or invalid. Configure GRYPHON_UCP_AGENT_PROFILE and omit the profile field "
    "to use that default, or supply a valid public HTTPS platform profile."
)
_STATIC_MESSAGES = {
    "capacity": "Execution capacity or resource budget exceeded",
    "timeout": "Execution timed out",
    "security": "Execution blocked by security policy",
    "validation": "Invalid source, inputs, schema, or JSON result",
    "conflict": "Request identity conflicts or cached catalog/profile is stale",
    "cache": "Execution storage is unavailable or the owned record was not found",
    "sandbox_unavailable": "Configured Docker sandbox is unavailable",
    "not_found": "Broker capability not found",
    "server_not_found": "Requested server not found",
    "function_not_found": "Requested function not found",
    "execution": "Sandbox execution failed",
    "upstream": "Upstream request failed",
    "internal": "Execution failed safely",
    "lint": "Code has syntax or lint issues",
    "cache_miss": "Owned cached recipe was not found",
    "cancelled": "Execution was cancelled",
    "interrupted": "Execution was interrupted",
    "context_limit": "Response exceeds context budget; narrow the request.",
}


class ExecutionDiagnostic(BaseModel):
    """Frozen allowlisted metadata; never accepts text, URLs, source, or extra fields."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, revalidate_instances="always", hide_input_in_errors=True
    )

    kind: Literal["upstream", "ast"]
    phase: DiagnosticPhase | None = None
    upstream_code: UpstreamCode | None = None
    violation_type: ASTViolationType | None = None
    line: int | None = Field(default=None, ge=1, le=MAX_DIAGNOSTIC_LINE)

    @model_validator(mode="after")
    def _consistent_kind(self) -> ExecutionDiagnostic:
        """Require exactly the metadata belonging to one diagnostic family."""
        if self.kind == "upstream":
            valid = self.phase is not None and self.violation_type is None and self.line is None
        else:
            valid = (
                self.phase is None
                and self.upstream_code is None
                and self.violation_type is not None
                and self.line is not None
            )
        if not valid:
            raise ValueError("Invalid execution diagnostic shape")
        return self


def validated_diagnostic(value: object) -> ExecutionDiagnostic | None:
    """Revalidate public metadata, including model instances bypassing normal construction."""
    try:
        return ExecutionDiagnostic.model_validate(value)
    except ValidationError:
        return None


def upstream_message(diagnostic: ExecutionDiagnostic) -> str:
    """Select static guidance for validated upstream metadata without reflecting unknown codes."""
    return UCP_PROFILE_MESSAGE if diagnostic.upstream_code == "invalid_profile_url" else _STATIC_MESSAGES["upstream"]


def canonical_failure(error_type: object, diagnostic: object = None) -> dict[str, object]:
    """Generate only owned static messages and validated category-compatible diagnostics.

    Args:
        error_type: Untrusted category, including values read from retained receipts.
        diagnostic: Optional untrusted structured metadata to validate afresh.

    Returns:
        A small failure envelope without any caller-provided error text.
    """
    kind = error_type if type(error_type) is str and error_type in _STATIC_MESSAGES else "internal"
    safe = validated_diagnostic(diagnostic)
    result: dict[str, object] = {"success": False, "error_type": kind, "error": _STATIC_MESSAGES[kind]}
    if safe is not None and (
        (safe.kind == "upstream" and kind == "upstream") or (safe.kind == "ast" and kind == "security")
    ):
        result["diagnostic"] = safe.model_dump(mode="json", exclude_none=True)
        if safe.kind == "upstream":
            result["error"] = upstream_message(safe)
    return result
