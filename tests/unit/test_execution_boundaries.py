"""Strict JSON, schema, source, and capability boundary regression tests."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gryphon.config import GryphonConfig
from gryphon.errors import DockerUnavailableError, ExecutionTimeoutError, InputValidationError, SecurityViolationError
from gryphon.models import ExecutionScope, RunRecord
from gryphon.runtime.context import guard_tool, public_result
from gryphon.runtime.execution_results import failure, fingerprint
from gryphon.runtime.execution_validation import json_bytes, prepare_source, request_digest, validate_inputs
from gryphon.runtime.sandboxes import RestrictedSandbox

if TYPE_CHECKING:
    import asyncio


def _config(**overrides: Any) -> GryphonConfig:
    """Validate isolated settings without reading an operator's environment file."""
    options: dict[str, Any] = {"_env_file": None, **overrides}
    return GryphonConfig(**options)


@pytest.mark.parametrize(
    "value, limit",
    [
        ("x" * 11, 10),
        ('"' * 10, 12),
        ("\ud800", 100),
        ([None] * 100_001, 1_000_000),
    ],
)
def test_json_size_unicode_and_node_limits_fail_without_coercion(value: Any, limit: int) -> None:
    with pytest.raises(InputValidationError):
        json_bytes(value, limit)


def test_json_cycles_fail_under_structural_depth_limit() -> None:
    value: list[Any] = []
    value.append(value)
    with pytest.raises(InputValidationError):
        json_bytes(value, 100_000)


@pytest.mark.parametrize(
    "inputs, schema",
    [
        ([], {}),
        ({}, []),
        ({}, {"type": "not-a-json-type"}),
        ({}, {"$schema": 42}),
        ({}, {"$schema": "http://json-schema.org/draft-03/schema#"}),
    ],
)
def test_input_contract_rejects_invalid_types_and_legacy_schema_dialects(inputs: Any, schema: Any) -> None:
    with pytest.raises(InputValidationError):
        validate_inputs(inputs, schema, _config())


@pytest.mark.parametrize(
    "schema",
    [
        {"properties": {"child": {"$ref": "https://example.invalid/schema"}}},
        {"items": {"$dynamicRef": "https://example.invalid/schema"}},
        {"$defs": {"child": {"$ref": "https://example.invalid/schema"}}},
        {"propertyNames": {"pattern": "(a+)+$"}},
        {"prefixItems": [{"$ref": "https://example.invalid/schema"}]},
        {"dependencies": {"child": {"$ref": "https://example.invalid/schema"}}},
        {"uniqueItems": True},
        {"contains": {}},
    ],
)
def test_all_schema_reference_and_unbounded_evaluation_routes_are_denied(schema: dict[str, Any]) -> None:
    with pytest.raises(InputValidationError):
        validate_inputs({}, schema, _config())


def test_schema_directive_names_are_safe_when_used_as_data_properties() -> None:
    schema = {"type": "object", "properties": {"pattern": {"type": "string"}, "$ref": {"type": "integer"}}}
    values, _ = validate_inputs({"pattern": "literal", "$ref": 42}, schema, _config())
    assert values == {"pattern": "literal", "$ref": 42}


def test_schema_annotation_values_never_act_as_reference_directives() -> None:
    values = {"$ref": "a literal value, not a schema reference"}
    actual, _ = validate_inputs(values, {"const": values}, _config())
    assert actual == values


def test_schema_structural_budget_rejects_deep_subschemas() -> None:
    schema: dict[str, Any] = {}
    for _ in range(8):
        schema = {"properties": {"child": schema}}
    with pytest.raises(InputValidationError):
        validate_inputs({}, schema, _config())


def test_schema_node_budget_rejects_large_annotation_trees() -> None:
    with pytest.raises(InputValidationError):
        validate_inputs({}, {"examples": [None] * 1025}, _config())


@pytest.mark.parametrize("source", ["", "result =", "print('only output')"])
def test_source_requires_valid_explicit_result_contract(source: str) -> None:
    with pytest.raises(InputValidationError):
        prepare_source(source, "restricted")


def test_docker_profile_rejects_broker_capability_before_container_admission() -> None:
    with pytest.raises(SecurityViolationError):
        prepare_source('result = await call_tool("weather.get", {})', "docker")


def test_direct_return_wrapper_never_calls_user_main_automatically() -> None:
    source = "def main():\n    return 99\nif inputs['ok']:\n    return 42\nreturn 0"
    prepared = prepare_source(source, "restricted")
    assert prepared.endswith("await _gryphon_execute()") and "await main()" not in prepared


def test_request_digest_tracks_exact_code_inputs_schema_catalog_and_profile() -> None:
    baseline = request_digest("result = 1", {}, {}, "catalog", "restricted")
    variants = [
        ("result=1", {}, {}, "catalog", "restricted"),
        ("result = 1", {"n": 1}, {}, "catalog", "restricted"),
        ("result = 1", {}, {"type": "object"}, "catalog", "restricted"),
        ("result = 1", {}, {}, "changed", "restricted"),
        ("result = 1", {}, {}, "catalog", "docker"),
    ]
    assert all(request_digest(*variant) != baseline for variant in variants)


@pytest.mark.parametrize(
    "capability, arguments",
    [
        ("missing_separator", {}),
        ("server.function.extra", {}),
        ("Server.function", {}),
        (42, {}),
        ("weather.get", []),
    ],
)
async def test_capability_shape_is_checked_before_broker_invocation(capability: Any, arguments: Any) -> None:
    broker = AsyncMock()
    backend = RestrictedSandbox(_config(), MagicMock(), broker)
    scope = ExecutionScope(run_id="unit", deadline=time.monotonic() + 10)
    state: dict[str, Any] = {"calls": 0, "failure": None}
    callbacks: set[asyncio.Task[Any]] = set()
    invoke = backend._capability(scope, callbacks, state)
    with pytest.raises(RuntimeError, match="Broker capability failed"):
        await invoke(capability, arguments)
    assert isinstance(state["failure"], InputValidationError) and scope.cancelled and not callbacks
    broker.invoke.assert_not_awaited()


async def test_expired_capability_authority_never_reaches_broker() -> None:
    broker = AsyncMock()
    backend = RestrictedSandbox(_config(), MagicMock(), broker)
    scope = ExecutionScope(run_id="unit", deadline=time.monotonic() - 1)
    state: dict[str, Any] = {"calls": 0, "failure": None}
    with pytest.raises(RuntimeError):
        await backend._capability(scope, set(), state)("weather.get", {})
    assert isinstance(state["failure"], ExecutionTimeoutError)
    broker.invoke.assert_not_awaited()


async def test_broker_response_arriving_after_deadline_is_not_returned_to_vm() -> None:
    broker = AsyncMock()
    backend = RestrictedSandbox(_config(), MagicMock(), broker)
    scope = ExecutionScope(run_id="unit", deadline=time.monotonic() + 10)
    state: dict[str, Any] = {"calls": 0, "failure": None}

    async def late(*args: Any) -> dict[str, Any]:
        """Model a response arriving after its execution authority expires."""
        scope.deadline = time.monotonic() - 1
        return {"private": "not returned"}

    broker.invoke.side_effect = late
    with pytest.raises(RuntimeError):
        await backend._capability(scope, set(), state)("weather.get", {})
    assert isinstance(state["failure"], ExecutionTimeoutError)


def test_print_callback_rejects_revoked_authority_without_buffering_values() -> None:
    backend = RestrictedSandbox(_config(), MagicMock(), AsyncMock())
    scope = ExecutionScope(run_id="unit", deadline=time.monotonic() + 10, cancelled=True)
    state: dict[str, Any] = {"printed": 0, "overflow": False, "failure": None}
    with pytest.raises(ExecutionTimeoutError):
        backend._printer(scope, state)("stdout", "private value")
    assert state["printed"] == 13 and "private value" not in str(state)


@pytest.mark.parametrize("receipt", [False, True])
def test_docker_unavailable_result_preserves_safe_category(receipt: bool) -> None:
    """Execution and persisted receipt responses retain actionable backend failure categories."""
    result = failure(DockerUnavailableError("private daemon details"), "run")
    record = RunRecord(id="run", request_hash="private", created_at=1, updated_at=1, result=result)
    payload = public_result(record if receipt else result)
    error = payload["result"] if receipt else payload
    assert not error["success"] and error["error_type"] == "sandbox_unavailable"
    assert "private daemon details" not in str(payload)


async def test_docker_unavailable_exception_preserves_safe_category() -> None:
    """The tool guard classifies backend failure before its generic execution base class."""

    async def unavailable() -> dict[str, Any]:
        """Simulate a backend failure without contacting a Docker daemon."""
        raise DockerUnavailableError("private daemon details")

    result = await guard_tool(unavailable, 1024)()
    assert not result["success"] and result["error_type"] == "sandbox_unavailable"
    assert "private daemon details" not in str(result)


@pytest.mark.parametrize("grants", [["weather.b", "weather.a"], ["weather.a", "weather.b", "weather.a"]])
def test_policy_fingerprint_equivalent_write_permits_preserve_identity(grants: list[str]) -> None:
    """Reordering or repeating exact permits does not change replay or idempotency authority."""
    registry = MagicMock()
    registry.fingerprint.return_value = "catalog"
    config = _config(allowed_write_operations=["weather.a", "weather.b"])
    identity = fingerprint(config, registry)
    config.allowed_write_operations = grants
    assert fingerprint(config, registry) == identity
    assert config.allowed_write_operations == grants


@pytest.mark.parametrize("grants", [[], ["weather.a"], ["weather.a", "weather.b", "weather.c"]])
def test_policy_fingerprint_changed_write_permits_invalidate_identity(grants: list[str]) -> None:
    """Adding or revoking authority still invalidates retained source identities."""
    registry = MagicMock()
    registry.fingerprint.return_value = "catalog"
    original = _config(allowed_write_operations=["weather.a", "weather.b"])
    changed = _config(allowed_write_operations=grants)
    assert fingerprint(original, registry) != fingerprint(changed, registry)
