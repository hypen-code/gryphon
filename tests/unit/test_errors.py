"""Unit tests for the Gryphon error hierarchy."""

from __future__ import annotations

import pytest

from gryphon.errors import (
    CacheError,
    CompileError,
    ConfigurationError,
    ExecutionError,
    ExecutionTimeoutError,
    FunctionNotFoundError,
    GryphonError,
    LintError,
    SecurityViolationError,
    ServerNotFoundError,
    SwaggerFetchError,
)

# ---------------------------------------------------------------------------
# Base error
# ---------------------------------------------------------------------------


def test_gryphon_error_is_exception() -> None:
    """GryphonError is an Exception subclass."""
    err = GryphonError("base error")
    assert isinstance(err, Exception)
    assert str(err) == "base error"


# ---------------------------------------------------------------------------
# CompileError / SwaggerFetchError
# ---------------------------------------------------------------------------


def test_compile_error_inherits_gryphon_error() -> None:
    err = CompileError("compilation failed")
    assert isinstance(err, GryphonError)
    assert "compilation failed" in str(err)


def test_swagger_fetch_error_inherits_compile_error() -> None:
    err = SwaggerFetchError("fetch failed")
    assert isinstance(err, CompileError)
    assert isinstance(err, GryphonError)


# ---------------------------------------------------------------------------
# SecurityViolationError
# ---------------------------------------------------------------------------


def test_security_violation_error() -> None:
    err = SecurityViolationError("dangerous import detected")
    assert isinstance(err, GryphonError)
    assert "dangerous import detected" in str(err)


# ---------------------------------------------------------------------------
# LintError
# ---------------------------------------------------------------------------


def test_lint_error_default_lint_output() -> None:
    err = LintError("code has issues")
    assert err.lint_output == ""
    assert str(err) == "code has issues"


def test_lint_error_with_lint_output() -> None:
    err = LintError("code has issues", lint_output="E501 line too long")
    assert err.lint_output == "E501 line too long"
    assert isinstance(err, GryphonError)


# ---------------------------------------------------------------------------
# ExecutionError / ExecutionTimeoutError
# ---------------------------------------------------------------------------


def test_execution_error_defaults() -> None:
    err = ExecutionError("docker failed")
    assert err.stderr == ""
    assert err.exit_code == 1
    assert str(err) == "docker failed"


def test_execution_error_with_details() -> None:
    err = ExecutionError("sandbox crashed", stderr="OOM killed", exit_code=137)
    assert err.stderr == "OOM killed"
    assert err.exit_code == 137
    assert isinstance(err, GryphonError)


def test_execution_timeout_error_inherits_execution_error() -> None:
    err = ExecutionTimeoutError("timed out after 30s", exit_code=124)
    assert isinstance(err, ExecutionError)
    assert isinstance(err, GryphonError)
    assert err.exit_code == 124


# ---------------------------------------------------------------------------
# CacheError
# ---------------------------------------------------------------------------


def test_cache_error() -> None:
    err = CacheError("DB locked")
    assert isinstance(err, GryphonError)
    assert "DB locked" in str(err)


# ---------------------------------------------------------------------------
# ServerNotFoundError / FunctionNotFoundError
# ---------------------------------------------------------------------------


def test_server_not_found_error() -> None:
    err = ServerNotFoundError("Server 'weather' not found")
    assert isinstance(err, GryphonError)
    assert "weather" in str(err)


def test_function_not_found_error() -> None:
    err = FunctionNotFoundError("Function 'foo' not in server 'bar'")
    assert isinstance(err, GryphonError)
    assert "foo" in str(err)


# ---------------------------------------------------------------------------
# ConfigurationError
# ---------------------------------------------------------------------------


def test_configuration_error() -> None:
    err = ConfigurationError("missing required env var")
    assert isinstance(err, GryphonError)


# ---------------------------------------------------------------------------
# raise / except integration
# ---------------------------------------------------------------------------


def test_raise_and_catch_lint_error() -> None:
    with pytest.raises(LintError) as exc_info:
        raise LintError("bad code", lint_output="W291 trailing whitespace")
    assert exc_info.value.lint_output == "W291 trailing whitespace"


def test_raise_and_catch_execution_timeout_error() -> None:
    with pytest.raises(ExecutionTimeoutError):
        raise ExecutionTimeoutError("timed out", exit_code=124)


def test_raise_and_catch_execution_error_as_gryphon_error() -> None:
    with pytest.raises(GryphonError):
        raise ExecutionError("failure")
