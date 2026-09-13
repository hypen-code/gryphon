"""Gryphon error hierarchy — all application exceptions defined here."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gryphon.models.diagnostics import ASTViolationType, ExecutionDiagnostic


class GryphonError(Exception):
    """Base error for all Gryphon exceptions."""


class CompileError(GryphonError):
    """Swagger parsing or code generation failure."""


class UCPImportError(CompileError):
    """UCP discovery or supported-tool adaptation failed with a static safe diagnostic."""


class SecurityViolationError(GryphonError):
    """Code failed security scan."""


class ASTViolationError(SecurityViolationError):
    """Static AST category and bounded source line without offending source text."""

    def __init__(self, violation_type: ASTViolationType, line: int) -> None:
        """Validate the closed diagnostic before storing or formatting any metadata."""
        from gryphon.models.diagnostics import ExecutionDiagnostic

        self._diagnostic = ExecutionDiagnostic(kind="ast", violation_type=violation_type, line=line)
        super().__init__(f"Security violation ({self._diagnostic.violation_type})")

    @property
    def diagnostic(self) -> ExecutionDiagnostic:
        """Return immutable metadata; public boundaries must still revalidate it."""
        return self._diagnostic


class LintError(GryphonError):
    """Code has syntax/lint issues."""

    def __init__(self, message: str, lint_output: str = "") -> None:
        super().__init__(message)
        self.lint_output = lint_output


class ExecutionError(GryphonError):
    """Sandbox execution failure."""

    def __init__(self, message: str, stderr: str = "", exit_code: int = 1) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.exit_code = exit_code


class UpstreamDiagnosticError(ExecutionError):
    """Trusted upstream failure carrying only closed, validated diagnostic metadata."""

    def __init__(self, diagnostic: ExecutionDiagnostic) -> None:
        """Accept only upstream metadata; never accept a raw message or response body."""
        from gryphon.models.diagnostics import ExecutionDiagnostic, upstream_message

        safe = ExecutionDiagnostic.model_validate(diagnostic)
        if safe.kind != "upstream":
            raise ValueError("Invalid upstream diagnostic kind")
        self._diagnostic = safe
        super().__init__(upstream_message(safe))

    @property
    def diagnostic(self) -> ExecutionDiagnostic:
        """Return immutable metadata; public boundaries must still revalidate it."""
        return self._diagnostic


class ExecutionTimeoutError(ExecutionError):
    """Code exceeded timeout."""


class DockerUnavailableError(ExecutionError):
    """Docker daemon is not running or not installed."""


class CacheError(GryphonError):
    """Cache read/write failure."""


class ServerNotFoundError(GryphonError):
    """Requested server does not exist."""


class FunctionNotFoundError(GryphonError):
    """Requested function does not exist in server."""


class ConfigurationError(GryphonError):
    """Invalid or missing configuration."""


class SwaggerFetchError(CompileError):
    """Failed to fetch or load swagger document."""


class InputValidationError(GryphonError):
    """An execution or capability argument violates its declared contract."""


class CapacityError(ExecutionError):
    """The configured admission or operation budget was exhausted."""


class ConflictError(GryphonError):
    """An idempotency key was reused for a different request."""


class SaaSStoreError(GryphonError):
    """Control-plane persistence failed without exposing backend details."""


class SaaSNotFoundError(SaaSStoreError):
    """The requested tenant-scoped resource does not exist."""


class SaaSDisabledError(SaaSStoreError):
    """A disabled tenant cannot mutate execution configuration."""


class SaaSQuotaError(SaaSStoreError):
    """A control-plane resource or listing quota would be exceeded."""


class SaaSValidationError(SaaSStoreError):
    """Control-plane input violates a bounded storage contract."""
