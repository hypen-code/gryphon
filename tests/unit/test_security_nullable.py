"""Broker integration tests for nullable manifest contracts and exact HTTP wire values."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest

from gryphon.errors import InputValidationError
from gryphon.models import EndpointManifest, ExecutionScope, ParamSchema, ServerManifest
from gryphon.runtime.registry import Registry
from gryphon.security.auth import AsyncVault
from gryphon.security.broker import ToolBroker
from gryphon.security.encoding import validate_arguments
from gryphon.security.network import NetworkClient
from gryphon.security.schema import validate_contract

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from gryphon.config import GryphonConfig


def _manifest() -> ServerManifest:
    """Create an in-memory endpoint without involving compiler filesystem fixtures."""
    endpoint = EndpointManifest(
        function_name="lookup",
        summary="",
        method="POST",
        path="/items",
        parameters_summary="",
        response_summary="",
        input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    )
    return ServerManifest(
        server_name="svc",
        description="",
        swagger_hash="mock",
        compiled_at="now",
        base_url="https://api.example.com",
        is_read_only=False,
        endpoints=[endpoint],
    )


@pytest.fixture
async def nullable_broker(
    gryphon_config: GryphonConfig,
) -> AsyncIterator[tuple[ToolBroker, EndpointManifest, list[httpx.Request]]]:
    """Use a mutable manifest and deterministic transport without upstream or DNS I/O."""
    manifest = _manifest()
    endpoint = manifest.endpoints[0]
    gryphon_config.allow_writes = True
    gryphon_config.allowed_write_operations = ["svc.lookup"]
    registry = MagicMock(spec=Registry)
    registry.get_manifest.return_value = manifest
    registry.get_endpoint.return_value = endpoint
    requests: list[httpx.Request] = []

    async def resolver(host: str, port: int) -> list[str]:
        """Supply a fixed public address, never DNS."""
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        """Record encoded wire values without making a connection."""
        requests.append(request)
        return httpx.Response(200, json={})

    broker = ToolBroker(gryphon_config, registry)
    await broker._network.close()
    broker._network = NetworkClient(gryphon_config, resolver=resolver, transport=httpx.MockTransport(handler))
    broker._vault = AsyncVault(broker._network)
    try:
        yield broker, endpoint, requests
    finally:
        await broker.close()


def _parameter(endpoint: EndpointManifest, location: str, required: bool, nullable: bool = True) -> str:
    """Preserve the full normalized schema in both manifest parameter representations."""
    name = "X-Page" if location == "header" else "wire-name"
    schema: dict[str, Any] = {"type": ["integer", "null"] if nullable else "integer", "minimum": 1}
    endpoint.parameters = [
        ParamSchema(name=name, location=location, required=required, param_type="integer", json_schema=schema),
    ]
    endpoint.input_schema["properties"][name] = schema
    if required:
        endpoint.input_schema["required"].append(name)
    if location == "path":
        endpoint.path = "/items/{" + name + "}"
    return name


def _body(endpoint: EndpointManifest, schema: dict[str, Any], required: bool = False) -> None:
    """Publish a body contract without erasing its explicit nullable type."""
    endpoint.request_body_schema = schema
    endpoint.input_schema["properties"]["json_body"] = schema
    if required:
        endpoint.input_schema["required"].append("json_body")


def _scope() -> ExecutionScope:
    """Create fresh host-owned invocation authority."""
    return ExecutionScope(run_id="nullable-test", deadline=time.monotonic() + 30)


@pytest.mark.parametrize("location", ["query", "header"])
@pytest.mark.parametrize("nullable", [True, False])
async def test_optional_wire_null_is_omitted_before_schema_validation(
    nullable_broker: tuple[ToolBroker, EndpointManifest, list[httpx.Request]],
    location: str,
    nullable: bool,
) -> None:
    """Optional None means absence, matching SDK filters even for nonnullable wire types."""
    broker, endpoint, requests = nullable_broker
    name = _parameter(endpoint, location, required=False, nullable=nullable)
    arguments = {name: None}
    normalized = validate_arguments(endpoint, arguments)
    validate_contract(normalized, endpoint.input_schema)
    await broker.invoke("svc", "lookup", arguments, _scope())
    assert normalized == {} and name not in requests[0].url.params and name not in requests[0].headers


@pytest.mark.parametrize("location", ["query", "header", "path"])
async def test_nullable_nonnull_wire_value_retains_declared_constraints(
    nullable_broker: tuple[ToolBroker, EndpointManifest, list[httpx.Request]],
    location: str,
) -> None:
    """A valid nonnull scalar keeps its original wire name and scalar representation."""
    broker, endpoint, requests = nullable_broker
    name = _parameter(endpoint, location, required=True)
    arguments = {name: 7}
    validate_contract(arguments, endpoint.input_schema)
    await broker.invoke("svc", "lookup", arguments, _scope())
    request = requests[0]
    actual = request.url.params.get(name) if location == "query" else request.headers.get(name)
    if location == "path":
        actual = request.url.path.rsplit("/", 1)[-1]
    assert actual == "7"


@pytest.mark.parametrize("location, required", [("query", True), ("header", True), ("path", True), ("path", False)])
async def test_required_or_path_null_is_rejected_before_upstream(
    nullable_broker: tuple[ToolBroker, EndpointManifest, list[httpx.Request]],
    location: str,
    required: bool,
) -> None:
    """A nullable schema cannot define an ambiguous required/path HTTP wire value."""
    broker, endpoint, requests = nullable_broker
    name = _parameter(endpoint, location, required=required)
    validate_contract({name: None}, endpoint.input_schema)
    with pytest.raises(InputValidationError, match="^Required or path wire parameters cannot be null$"):
        await broker.invoke("svc", "lookup", {name: None}, _scope())
    assert not requests


async def test_input_schema_required_cannot_be_bypassed_by_optional_metadata(
    nullable_broker: tuple[ToolBroker, EndpointManifest, list[httpx.Request]],
) -> None:
    """Both required declarations are authoritative when rejecting wire nulls."""
    broker, endpoint, requests = nullable_broker
    name = _parameter(endpoint, "query", required=False)
    endpoint.input_schema["required"].append(name)
    with pytest.raises(InputValidationError, match="Required or path"):
        await broker.invoke("svc", "lookup", {name: None}, _scope())
    assert not requests


@pytest.mark.parametrize("value", [0, "sensitive-invalid-value"])
async def test_nullable_schema_does_not_erase_nonnull_constraints(
    nullable_broker: tuple[ToolBroker, EndpointManifest, list[httpx.Request]],
    value: Any,
) -> None:
    """Nullable types still enforce minimum bounds and reject scalar coercion."""
    broker, endpoint, requests = nullable_broker
    name = _parameter(endpoint, "query", required=False)
    with pytest.raises(
        InputValidationError, match="^Arguments or schema are invalid; schemas must be resolved and bounded$"
    ):
        await broker.invoke("svc", "lookup", {name: value}, _scope())
    assert not requests


@pytest.mark.parametrize("location", ["query", "header", "path"])
async def test_null_array_items_have_no_nonbody_wire_encoding(
    nullable_broker: tuple[ToolBroker, EndpointManifest, list[httpx.Request]],
    location: str,
) -> None:
    """Nullable array elements are rejected rather than omitted, stringified or flattened."""
    broker, endpoint, requests = nullable_broker
    name = _parameter(endpoint, location, required=False)
    schema = {"type": "array", "items": {"type": ["integer", "null"]}}
    endpoint.parameters[0].json_schema = schema
    endpoint.input_schema["properties"][name] = schema
    validate_contract({name: [1, None]}, endpoint.input_schema)
    with pytest.raises(InputValidationError, match="^Null array items have no supported HTTP wire encoding$"):
        await broker.invoke("svc", "lookup", {name: [1, None]}, _scope())
    assert not requests


@pytest.mark.parametrize("legacy_body_parameter", [False, True])
async def test_declared_nullable_body_sends_literal_json_null(
    nullable_broker: tuple[ToolBroker, EndpointManifest, list[httpx.Request]],
    legacy_body_parameter: bool,
) -> None:
    """Explicit body None is JSON null, including required nullable Swagger body parameters."""
    broker, endpoint, requests = nullable_broker
    schema = {"type": ["object", "null"]}
    _body(endpoint, schema, required=True)
    if legacy_body_parameter:
        endpoint.parameters = [
            ParamSchema(name="json_body", location="body", param_type="object", required=True, json_schema=schema),
        ]
    arguments = {"json_body": None}
    assert validate_arguments(endpoint, arguments) == arguments
    await broker.invoke("svc", "lookup", arguments, _scope())
    assert requests[0].content == b"null" and requests[0].headers["Content-Type"] == "application/json"


async def test_omitted_nullable_body_still_has_no_request_body(
    nullable_broker: tuple[ToolBroker, EndpointManifest, list[httpx.Request]],
) -> None:
    """An absent optional JSON body is not silently changed into an explicit null."""
    broker, endpoint, requests = nullable_broker
    _body(endpoint, {"type": ["object", "null"]})
    await broker.invoke("svc", "lookup", {}, _scope())
    assert requests[0].content == b"" and "Content-Type" not in requests[0].headers


async def test_nonnullable_body_rejects_explicit_null(
    nullable_broker: tuple[ToolBroker, EndpointManifest, list[httpx.Request]],
) -> None:
    """Body null is permitted only when the authoritative input contract admits it."""
    broker, endpoint, requests = nullable_broker
    _body(endpoint, {"type": "object"})
    with pytest.raises(InputValidationError):
        await broker.invoke("svc", "lookup", {"json_body": None}, _scope())
    assert not requests


async def test_nullable_json_array_items_are_preserved_in_body(
    nullable_broker: tuple[ToolBroker, EndpointManifest, list[httpx.Request]],
) -> None:
    """JSON, unlike scalar HTTP parameters, has an unambiguous null element representation."""
    broker, endpoint, requests = nullable_broker
    _body(endpoint, {"type": "array", "items": {"type": ["integer", "null"]}})
    arguments = {"json_body": [1, None]}
    validate_contract(arguments, endpoint.input_schema)
    await broker.invoke("svc", "lookup", arguments, _scope())
    assert requests[0].content == b"[1,null]"
