"""Gryphon error hierarchy — all application exceptions defined here."""

from __future__ import annotations


class GryphonError(Exception):
    """Base error for all Gryphon exceptions."""


class CompileError(GryphonError):
    """Swagger parsing or code generation failure."""


class UCPImportError(CompileError):
    """UCP discovery or supported-tool adaptation failed with a static safe diagnostic."""


class SecurityViolationError(GryphonError):
    """Code failed security scan."""


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
