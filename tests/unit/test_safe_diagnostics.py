"""Closed upstream failure metadata cannot reflect remote messages or forged receipts."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import ValidationError

from gryphon.errors import ExecutionError, UpstreamDiagnosticError
from gryphon.models import ExecutionDiagnostic, ExecutionResult, ExecutionScope
from gryphon.models.diagnostics import UCP_PROFILE_MESSAGE, canonical_failure, validated_diagnostic
from gryphon.runtime.execution_results import failure, fingerprint
from gryphon.runtime.registry import Registry
from gryphon.runtime.sandboxes import RestrictedSandbox
from gryphon.security.mcp_protocol import SSEDecoder, consume_sse, rpc_record

if TYPE_CHECKING:
    from pathlib import Path

    from gryphon.config import GryphonConfig
    from gryphon.models.diagnostics import DiagnosticPhase, UpstreamCode

_PRIVATE = "synthetic-private-value"


def _record(code: object = "invalid_profile_url") -> dict[str, Any]:
    """Return synthetic sensitive upstream data under an owned response identifier."""
    return {
        "jsonrpc": "2.0",
        "id": "fixture",
        "error": {
            "code": -32001,
            "message": _PRIVATE,
            "data": {
                "code": code,
                "content": {"nested": [_PRIVATE]},
                "continue_url": "https://example.com/?token=" + _PRIVATE,
                "session": _PRIVATE,
            },
        },
    }


def test_known_upstream_code_has_static_actionable_failure() -> None:
    """Keep only the known enum and host phase, not error messages or nested data."""
    with pytest.raises(UpstreamDiagnosticError) as caught:
        rpc_record(_record(), "fixture", phase="discovery")
    result = failure(caught.value)
    assert result.error == UCP_PROFILE_MESSAGE
    assert result.error_type == "upstream"
    assert result.diagnostic == ExecutionDiagnostic(
        kind="upstream", upstream_code="invalid_profile_url", phase="discovery"
    )
    assert _PRIVATE not in result.model_dump_json()
    assert _PRIVATE not in str(caught.value)
    assert result == ExecutionResult.model_validate_json(result.model_dump_json())


@pytest.mark.parametrize(
    "code",
    [
        _PRIVATE,
        "invalid_profile_url" + _PRIVATE,
        "invalid_profile_url\n",
        "invalid_profile_url\x00",
        "INVALID_PROFILE_URL",
        " invalid_profile_url",
        "invalid_profile_url invalid_profile_url",
        "invalid_profile_urlé",
        "іnvalid_profile_url",
        "x" * 65,
        "invalid_profile_url" * 10000,
        "aHR0cHM6Ly9wcml2YXRlLmludmFsaWQv",
        "https://private.invalid/profile?token=" + _PRIVATE,
        {"code": "invalid_profile_url"},
        ["invalid_profile_url"],
        1,
        True,
        None,
    ],
)
def test_unknown_upstream_codes_keep_only_phase(code: object) -> None:
    """Unknown codes remain private while valid RPC errors retain their upstream category."""
    with pytest.raises(UpstreamDiagnosticError) as caught:
        rpc_record(_record(code), "fixture")
    result = failure(caught.value)
    assert result.diagnostic == ExecutionDiagnostic(kind="upstream", phase="invoke")
    assert result.error_type == "upstream"
    assert result.error == "Upstream request failed"
    assert str(caught.value) == "Upstream request failed"
    assert result.diagnostic.model_dump(exclude_none=True) == {"kind": "upstream", "phase": "invoke"}
    assert _PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize(
    "error",
    [
        None,
        [],
        {"data": {"code": "invalid_profile_url"}},
        {"code": True, "message": _PRIVATE},
        {"code": -(2**31) - 1, "message": _PRIVATE},
        {"code": 2**31, "message": _PRIVATE},
        {"code": "-32001", "message": _PRIVATE},
        {"code": -32001.0, "message": _PRIVATE},
        {"code": -32001, "message": {"private": _PRIVATE}},
        {"code": -32001},
    ],
)
def test_malformed_rpc_errors_are_not_domain_diagnostics(error: object) -> None:
    """A malformed JSON-RPC error cannot become a recognized UCP failure."""
    record = _record()
    record["error"] = error
    with pytest.raises(ExecutionError) as caught:
        rpc_record(record, "fixture")
    assert type(caught.value) is ExecutionError


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "foreign"},
        {"id": 1},
        {"jsonrpc": "1.0"},
        {"result": {}},
        {"method": "notifications/message"},
    ],
)
def test_known_code_cannot_bypass_rpc_identity(changes: dict[str, object]) -> None:
    """Classify only after all protocol and exact response-identity checks pass."""
    with pytest.raises(ExecutionError) as caught:
        rpc_record({**_record(), **changes}, "fixture")
    assert type(caught.value) is ExecutionError


@pytest.mark.parametrize("content", [_PRIVATE, "\x00\n" + _PRIVATE, {"code": "other", "data": [_PRIVATE]}, "x" * 10000])
def test_known_code_ignores_all_other_error_data(content: object) -> None:
    """Even nested misleading codes and encoded/control content are never reflected."""
    record = _record()
    record["error"]["data"]["content"] = content
    with pytest.raises(UpstreamDiagnosticError) as caught:
        rpc_record(record, "fixture")
    assert failure(caught.value).error == UCP_PROFILE_MESSAGE
    assert _PRIVATE not in failure(caught.value).model_dump_json()


@pytest.mark.parametrize("phase", ["discovery", "invoke"])
@pytest.mark.parametrize("code", [-(2**31), -32001, 0, 2**31 - 1])
def test_valid_rpc_error_without_domain_code_has_phase_only_diagnostic(code: int, phase: DiagnosticPhase) -> None:
    """Signed 32-bit JSON-RPC failures remain actionable upstream failures without leaking their code."""
    record = {"jsonrpc": "2.0", "id": "fixture", "error": {"code": code, "message": _PRIVATE}}
    with pytest.raises(UpstreamDiagnosticError) as caught:
        rpc_record(record, "fixture", phase=phase)
    result = failure(caught.value)
    assert result.error_type == "upstream"
    assert result.error == "Upstream request failed"
    assert result.diagnostic == ExecutionDiagnostic(kind="upstream", phase=phase)
    assert canonical_failure(result.error_type, result.diagnostic) == {
        "success": False,
        "error_type": "upstream",
        "error": "Upstream request failed",
        "diagnostic": {"kind": "upstream", "phase": phase},
    }
    assert _PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize("data", [None, [], _PRIVATE, {"nested": {"code": "invalid_profile_url"}}])
def test_valid_rpc_error_with_unstructured_data_keeps_only_phase(data: object) -> None:
    """Opaque optional error data neither changes classification nor enters the safe diagnostic."""
    record = _record()
    record["error"]["data"] = data
    with pytest.raises(UpstreamDiagnosticError) as caught:
        rpc_record(record, "fixture")
    assert caught.value.diagnostic == ExecutionDiagnostic(kind="upstream", phase="invoke")
    assert _PRIVATE not in failure(caught.value).model_dump_json()


def test_sse_unknown_error_keeps_only_phase() -> None:
    """SSE errors with unknown domain codes preserve the same upstream provenance as JSON."""
    decoder = SSEDecoder("fixture", phase="discovery")
    with pytest.raises(UpstreamDiagnosticError) as caught:
        decoder.feed(b"data: " + json.dumps(_record(_PRIVATE)).encode() + b"\n\n")
    assert caught.value.diagnostic == ExecutionDiagnostic(kind="upstream", phase="discovery")
    assert failure(caught.value).error == "Upstream request failed"
    assert _PRIVATE not in str(caught.value)


def test_sse_error_preserves_only_safe_phase_and_code() -> None:
    """Incremental SSE uses the same allowlist and host-selected discovery phase."""
    decoder = SSEDecoder("fixture", phase="discovery")
    record = b"data: " + json.dumps(_record()).encode() + b"\n\n"
    decoder.feed(record[:17])
    with pytest.raises(UpstreamDiagnosticError) as caught:
        decoder.feed(record[17:])
    assert caught.value.diagnostic.phase == "discovery"
    assert _PRIVATE not in failure(caught.value).model_dump_json()


async def test_consumed_sse_error_preserves_discovery_phase() -> None:
    """Bounded response streaming does not silently change diagnostic provenance."""
    response = httpx.Response(200, content=b"data: " + json.dumps(_record()).encode() + b"\n\n")
    with pytest.raises(UpstreamDiagnosticError) as caught:
        await consume_sse(response, 4096, "fixture", phase="discovery")
    assert caught.value.diagnostic.phase == "discovery"


@pytest.mark.parametrize(
    "changes",
    [
        {"upstream_code": _PRIVATE},
        {"phase": _PRIVATE},
        {"phase": "validation"},
        {"kind": _PRIVATE},
        {"message": _PRIVATE},
        {"cache_id": _PRIVATE},
        {"continue_url": _PRIVATE},
        {"line": 1},
        {"violation_type": "blocked_import"},
        {"phase": None},
    ],
)
def test_diagnostic_model_rejects_non_allowlisted_metadata(changes: dict[str, object]) -> None:
    """Public model validation must reject free text, mixed families, and extra fields."""
    value = {"kind": "upstream", "phase": "invoke", "upstream_code": "invalid_profile_url", **changes}
    with pytest.raises(ValidationError):
        ExecutionDiagnostic.model_validate(value)
    assert validated_diagnostic(value) is None
    assert "diagnostic" not in canonical_failure("upstream", value)


def test_diagnostic_is_frozen_and_revalidates_bypassed_construction() -> None:
    """Normal mutation is denied and model_construct cannot bless arbitrary metadata."""
    safe = ExecutionDiagnostic(kind="upstream", phase="invoke", upstream_code="invalid_profile_url")
    with pytest.raises(ValidationError):
        safe.phase = "discovery"
    forged = safe.model_copy(update={"upstream_code": _PRIVATE})
    assert validated_diagnostic(forged) is None
    assert canonical_failure("upstream", forged)["error"] == "Upstream request failed"
    with pytest.raises(ValidationError):
        ExecutionResult.model_validate({"success": False, "diagnostic": forged})


def test_mutated_diagnostic_cannot_leak_extra_cache_or_session_fields() -> None:
    """Revalidation catches dictionary-level tampering, not only known field mutation."""
    safe = ExecutionDiagnostic(kind="upstream", phase="invoke", upstream_code="invalid_profile_url")
    error = UpstreamDiagnosticError(safe)
    error.diagnostic.__dict__["cache_id"] = _PRIVATE
    error.diagnostic.__dict__["session"] = _PRIVATE
    result = failure(error)
    assert result.diagnostic is None
    assert _PRIVATE not in result.model_dump_json()


def test_upstream_exception_rejects_ast_family() -> None:
    """Trusted exception constructors do not allow unrelated diagnostic families."""
    with pytest.raises(ValueError, match="Invalid upstream diagnostic kind"):
        UpstreamDiagnosticError(ExecutionDiagnostic(kind="ast", violation_type="blocked_import", line=1))


@pytest.mark.parametrize("kind", [_PRIVATE, "security\n", {"kind": "upstream"}, None, ["upstream"]])
def test_canonical_failure_rejects_untrusted_categories(kind: object) -> None:
    """Static message selection never reflects the category supplied by a receipt."""
    result = canonical_failure(kind)
    assert result["error_type"] == "internal"
    assert _PRIVATE not in json.dumps(result)


def test_failure_ignores_exception_text_and_untrusted_diagnostic_attributes() -> None:
    """Ordinary exceptions cannot opt into upstream classification by adding attributes."""
    error = ExecutionError(_PRIVATE, stderr=_PRIVATE)
    error.__dict__["diagnostic"] = {"kind": "upstream", "phase": "invoke", "upstream_code": "invalid_profile_url"}
    result = failure(error)
    assert result.error_type == "execution"
    assert result.diagnostic is None
    assert _PRIVATE not in result.model_dump_json()


def test_canonical_failure_drops_category_mismatched_diagnostics() -> None:
    """A legitimate diagnostic cannot be attached to an unrelated public error type."""
    diagnostic = ExecutionDiagnostic(kind="upstream", phase="invoke", upstream_code="invalid_profile_url")
    assert "diagnostic" not in canonical_failure("security", diagnostic)
    assert canonical_failure("upstream", diagnostic)["error"] == UCP_PROFILE_MESSAGE


def test_execution_fingerprint_changes_with_operator_profile(tmp_path: Path, gryphon_config: GryphonConfig) -> None:
    """Configured platform identity participates in replay identity without exposing its URI."""
    registry = Registry(str(tmp_path))
    config = gryphon_config.model_copy(update={"ucp_agent_profile": None})
    first = fingerprint(config, registry)
    profile = "https://platform.example/profile"
    changed = config.model_copy(update={"ucp_agent_profile": profile})
    second = fingerprint(changed, registry)
    assert first != second
    assert second == fingerprint(changed, registry)
    assert profile not in second
    assert len(first) == len(second) == 64


@pytest.mark.parametrize(
    "code",
    [
        'result = await call_tool("fixture.lookup", {})\nresult',
        'try:\n    result = await call_tool("fixture.lookup", {})\nexcept Exception:\n    result = 1\nresult',
    ],
)
@pytest.mark.parametrize("upstream_code", ["invalid_profile_url", None])
async def test_monty_preserves_safe_diagnostic_even_when_source_catches_failure(
    code: str, gryphon_config: GryphonConfig, upstream_code: UpstreamCode | None
) -> None:
    """The host-retained cause survives VM error wrapping and cannot become sandbox success."""
    diagnostic = ExecutionDiagnostic(kind="upstream", phase="invoke", upstream_code=upstream_code)
    error = UpstreamDiagnosticError(diagnostic)
    broker = AsyncMock()
    broker.invoke.side_effect = error
    backend = RestrictedSandbox(gryphon_config, MagicMock(), broker)
    scope = ExecutionScope(run_id="fixture", deadline=time.monotonic() + 5)
    with pytest.raises(UpstreamDiagnosticError) as caught:
        await backend.run(code, {}, scope)
    assert caught.value is error
    result = failure(caught.value)
    assert result.diagnostic == diagnostic
    assert result.error == (UCP_PROFILE_MESSAGE if upstream_code else "Upstream request failed")
    assert result.error_type == "upstream"
    assert scope.cancelled
    broker.invoke.assert_awaited_once()
