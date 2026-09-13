"""Public diagnostic validation and actionable bounded artifact handoff."""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

import pytest

from gryphon.errors import ASTViolationError, UpstreamDiagnosticError
from gryphon.models import ExecutionDiagnostic, ExecutionResult, RunRecord
from gryphon.runtime.artifacts import ArtifactStore
from gryphon.runtime.context import bounded_json, bounded_receipt, json_bytes, public_result, safe_error
from gryphon.runtime.execution_results import bound_result, failure

if TYPE_CHECKING:
    from pathlib import Path

    from gryphon.config import GryphonConfig

SECRET = "Bearer private-fixture credential-value"


@pytest.mark.parametrize("phase", ["discovery", "invoke"])
async def test_canonical_profile_diagnostics_survive_public_and_retained_receipts(phase: str, tmp_path: Path) -> None:
    """Safe static guidance survives both response paths without any arbitrary error text."""
    diagnostic = ExecutionDiagnostic.model_validate(
        {"kind": "upstream", "phase": phase, "upstream_code": "invalid_profile_url"}
    )
    error = UpstreamDiagnosticError(diagnostic)
    result = failure(error, "r" * 32)
    direct = public_result(result)
    assert direct["error"] == result.error and direct["error_type"] == "upstream"
    assert direct["diagnostic"] == diagnostic.model_dump(exclude_none=True)
    assert safe_error(error)["error"] == result.error
    record = RunRecord(
        id="r" * 32, owner="owner", request_hash=SECRET, status="failed", created_at=1, updated_at=2, result=result
    )
    receipt = await bounded_receipt(record, ArtifactStore(str(tmp_path), 10), "owner", 1024)
    assert receipt["result"]["error"] == result.error and receipt["result"]["diagnostic"] == direct["diagnostic"]
    assert SECRET not in str(receipt) and "request_hash" not in receipt


@pytest.mark.parametrize(
    "diagnostic",
    [
        {"kind": "upstream", "phase": "invoke", "upstream_code": SECRET},
        {"kind": "upstream", "phase": "invoke", "upstream_code": "invalid_profile_url", "message": SECRET},
        {"kind": "ast", "violation_type": SECRET, "line": 2},
        {"kind": "ast", "violation_type": "blocked_attribute", "line": SECRET},
    ],
)
def test_public_boundary_rejects_forged_diagnostics_without_serializer_warning(diagnostic: dict[str, Any]) -> None:
    """Bypassing a model constructor cannot leak values through output or serializer warnings."""
    result = ExecutionResult.model_construct(
        success=False,
        error_type="upstream",
        error=SECRET,
        diagnostic=diagnostic,
        data=SECRET,
        prints=SECRET,
        traceback=SECRET,
    )
    record = RunRecord(id="r", owner="owner", request_hash="hash", created_at=1, updated_at=2)
    record = record.model_copy(update={"result": result})
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        direct = public_result(result)
        nested = public_result(record)
    assert not emitted
    assert SECRET not in str(direct) + str(nested)
    assert "diagnostic" not in direct and "diagnostic" not in nested["result"]
    assert direct["error"] == "Upstream request failed"


def test_ast_failure_keeps_type_line_and_static_message() -> None:
    """No offending attribute or source detail is needed to identify the blocked source line."""
    result = failure(ASTViolationError("blocked_attribute", 2))
    public = public_result(result)
    assert public["error"] == result.error
    assert public["diagnostic"] == {"kind": "ast", "violation_type": "blocked_attribute", "line": 2}
    assert safe_error(ASTViolationError("blocked_attribute", 2))["diagnostic"] == public["diagnostic"]


async def test_artifact_summary_and_projection_hint_fit_minimum_execution_budget(
    gryphon_config: GryphonConfig,
    tmp_path: Path,
) -> None:
    """Many long keys cannot consume response space needed for an actionable artifact handle."""
    config = gryphon_config.model_copy(update={"max_output_size_bytes": 1024})
    store = ArtifactStore(str(tmp_path), 10)
    value = {f"key-{index}-" + "x" * 70: "v" * 5000 for index in range(40)}
    result = await bound_result(ExecutionResult(success=True, data=value), "owner", config, store)
    public = bounded_json(public_result(result), 1024)
    assert len(json_bytes(public)) <= 1024
    assert public["artifact_id"] and public["next"]["tool"] == "transform_artifact"
    assert public["data"]["json_type"] == "object" and public["data"]["keys_truncated"]
    assert public["data"]["key_count"] == 40
    assert set(public["data"]["top_level_keys"]) <= value.keys()


async def test_receipt_artifact_handoff_preserves_root_keys_and_local_next_step(tmp_path: Path) -> None:
    """Data exceeding the receipt budget is projectable without retrieving or refetching all bytes."""
    result = ExecutionResult(success=True, data={"products": ["x" * 20000]}, cache_id="c" * 64)
    record = RunRecord(id="r", owner="owner", request_hash="h", created_at=1, updated_at=2, result=result)
    receipt = await bounded_receipt(record, ArtifactStore(str(tmp_path), 10), "owner", 1024)
    nested = receipt["result"]
    assert nested["artifact_id"] and nested["data"]["top_level_keys"] == ["products"]
    assert nested["next"]["tool"] == "transform_artifact"
    assert result.artifact_id is None
