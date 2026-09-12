"""AST failures expose only a bounded line and a fixed violation category."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gryphon.errors import ASTViolationError
from gryphon.models import ExecutionDiagnostic
from gryphon.models.diagnostics import MAX_DIAGNOSTIC_LINE, canonical_failure
from gryphon.runtime.execution_results import failure
from gryphon.security.ast_guard import ASTGuard


@pytest.mark.parametrize(
    ("source", "violation_type", "line"),
    [
        ("result = 1\nresult = type(inputs).__name__", "blocked_attribute", 2),
        ("result = type(inputs).__name__", "blocked_attribute", 1),
        ("result = 1\nimport private_module", "blocked_import", 2),
        ("from private_module import private_name", "blocked_import", 1),
        ("from json import _private_name", "blocked_import", 1),
        ("result = open('synthetic-private-path')", "blocked_call", 1),
        ("result = object.system('synthetic-private-command')", "blocked_attribute_call", 1),
        ("result = open", "blocked_call", 1),
        ("def f():\n    global private_name", "blocked_global", 2),
        ("def f():\n    nonlocal private_name", "blocked_nonlocal", 2),
    ],
)
def test_ast_violations_have_closed_categories_and_exact_lines(source: str, violation_type: str, line: int) -> None:
    """Preserve useful source position without source, names, paths, or arguments."""
    with pytest.raises(ASTViolationError) as caught:
        ASTGuard().validate(source)
    result = failure(caught.value)
    assert result.error_type == "security"
    assert result.diagnostic is not None
    assert result.diagnostic.violation_type == violation_type
    assert result.diagnostic.line == line
    assert result.diagnostic.kind == "ast"
    assert result.diagnostic.phase is None
    assert "private" not in result.model_dump_json()
    assert "private" not in str(caught.value)
    assert source not in result.model_dump_json()


@pytest.mark.parametrize("line", [0, -1, MAX_DIAGNOSTIC_LINE + 1, True, "2", 1.0, None])
def test_ast_line_is_a_strict_bounded_positive_integer(line: object) -> None:
    """Reject coercion, booleans, missing positions and unbounded values."""
    with pytest.raises(ValidationError):
        ExecutionDiagnostic.model_validate({"kind": "ast", "violation_type": "blocked_attribute", "line": line})


def test_ast_unknown_violation_type_is_rejected() -> None:
    """An exception category cannot contain offending source or arbitrary metadata."""
    with pytest.raises(ValidationError):
        ExecutionDiagnostic.model_validate({"kind": "ast", "violation_type": "synthetic-private", "line": 1})


def test_ast_first_violation_wins_without_unbounded_diagnostic_collection() -> None:
    """Additional failures neither replace the first source location nor expand output."""
    with pytest.raises(ASTViolationError) as caught:
        ASTGuard().validate("result = 1\nresult = inputs.__name__\n" + "import private_module\n" * 100)
    diagnostic = caught.value.diagnostic
    assert diagnostic.violation_type == "blocked_attribute"
    assert diagnostic.line == 2
    assert len(failure(caught.value).model_dump_json()) < 512


def test_ast_exception_diagnostic_is_read_only() -> None:
    """The public property does not permit replacing trusted metadata with raw values."""
    error = ASTViolationError("blocked_attribute", 2)
    with pytest.raises(AttributeError):
        object.__setattr__(error, "diagnostic", "synthetic-private")
    with pytest.raises(ValidationError):
        error.diagnostic.line = 3


def test_ast_forged_diagnostic_is_removed_at_failure_boundary() -> None:
    """Private storage mutation cannot introduce names or cache fields to public output."""
    error = ASTViolationError("blocked_attribute", 2)
    error.diagnostic.__dict__["violation_type"] = "synthetic-private"
    error.diagnostic.__dict__["cache_id"] = "synthetic-private"
    result = failure(error)
    assert result.error_type == "security"
    assert result.diagnostic is None
    assert "synthetic-private" not in result.model_dump_json()


def test_ast_diagnostic_cannot_claim_upstream_phase() -> None:
    """Validation excludes phase/code from AST metadata rather than exposing a hybrid."""
    value = {"kind": "ast", "violation_type": "blocked_attribute", "line": 2, "phase": "invoke"}
    with pytest.raises(ValidationError):
        ExecutionDiagnostic.model_validate(value)
    assert "diagnostic" not in canonical_failure("security", value)
