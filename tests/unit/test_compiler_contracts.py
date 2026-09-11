"""Native v2 schemas, broker compatibility and explicit wire-subset regressions."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator

from gryphon.compiler.catalog import input_schema, load_manifest, validate_endpoint
from gryphon.compiler.codegen import CodeGenerator, _build_param_annotation, _build_return_type
from gryphon.compiler.orchestrator import Orchestrator
from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.errors import CompileError
from gryphon.models import EndpointSpec, ParamSchema, ServerSpec, SwaggerSource
from gryphon.runtime.registry import Registry
from gryphon.security.encoding import encode_request, validate_arguments
from gryphon.security.schema import check_schema

if TYPE_CHECKING:
    from pathlib import Path

    from gryphon.config import GryphonConfig


def _parser() -> SwaggerParser:
    """Construct an offline parser for structured contract tests without inline YAML."""
    return SwaggerParser(SwaggerSource(name="test", swagger_url="unused", base_url="https://example.com"))


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "number", "minimum": -90, "maximum": 90, "multipleOf": 0.5},
        {"type": "integer", "exclusiveMinimum": 0, "maximum": 5, "default": 1},
        {"type": ["string", "null"], "minLength": 2, "maxLength": 5, "enum": ["value", None], "default": None},
        {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 3},
        {"type": ["array", "null"], "items": {"type": "boolean"}, "maxItems": 3},
    ],
)
def test_parameter_native_constraints_preserved(schema: dict[str, Any]) -> None:
    """Normalization and input-schema construction retain every supported native constraint."""
    endpoint = _parser()._parse_operation(
        "/x",
        "GET",
        {
            "parameters": [
                {"name": "values", "in": "query", "schema": schema},
            ]
        },
        [],
    )
    assert endpoint is not None
    assert endpoint.parameters[0].json_schema == schema
    assert input_schema(endpoint)["properties"]["values"] == schema
    check_schema(input_schema(endpoint))


def test_swagger_array_top_level_bounds_preserved() -> None:
    """Swagger 2.0 scalar-array items and bounds survive multi-key normalization."""
    parser = _parser()
    parser._raw_doc = {"swagger": "2.0"}
    parameter = parser._parse_parameters(
        [
            {
                "name": "ids",
                "in": "query",
                "type": "array",
                "collectionFormat": "multi",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 1,
            }
        ]
    )[0]
    assert parameter.json_schema == {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1}


@pytest.mark.parametrize(
    "raw",
    [
        {"schema": {"type": "array", "items": {"type": "string"}}, "explode": False},
        {"schema": {"type": "array", "items": {"type": "object"}}},
        {"schema": {"type": "array"}},
        {"schema": {"type": "object"}},
        {"schema": {"properties": {"name": {"type": "string"}}}},
        {"schema": {"type": "string"}, "style": "spaceDelimited"},
        {"schema": {"type": "string"}, "style": "deepObject"},
        {"schema": {"type": "string"}, "allowReserved": True},
        {"schema": {"type": "string"}, "explode": "true"},
        {"type": "string", "pattern": "^value$"},
    ],
)
def test_unsupported_wire_styles_and_legacy_constraints_rejected(raw: dict[str, Any]) -> None:
    """Unsupported serialization and Swagger validation semantics never disappear silently."""
    with pytest.raises(CompileError):
        _parser()._parse_parameters([{"name": "value", "in": "query", **raw}])


@pytest.mark.parametrize(
    "keyword",
    [
        "oneOf",
        "allOf",
        "anyOf",
        "not",
        "discriminator",
        "if",
        "then",
        "else",
        "$dynamicRef",
        "$recursiveRef",
        "$schema",
        "$id",
        "dependentSchemas",
        "dependentRequired",
        "pattern",
        "patternProperties",
        "propertyNames",
        "contains",
        "prefixItems",
        "unevaluatedProperties",
        "uniqueItems",
    ],
)
def test_unsupported_schema_semantics_rejected(keyword: str) -> None:
    """The compiler cannot advertise a contract the bounded broker validator will reject."""
    with pytest.raises(CompileError):
        _parser()._normalize_schema({"type": "string", keyword: {}})


def test_nullable_and_swagger_exclusive_bounds_normalized() -> None:
    """Legacy nullable and exclusive-bound booleans become native JSON Schema constraints."""
    assert _parser()._normalize_schema(
        {
            "type": "number",
            "nullable": True,
            "minimum": 1,
            "exclusiveMinimum": True,
            "maximum": 10,
            "exclusiveMaximum": False,
        }
    ) == {"type": ["number", "null"], "exclusiveMinimum": 1, "maximum": 10}


def test_data_named_ref_is_not_interpreted_as_schema_reference() -> None:
    """JSON property/default keys are data, even when their spelling matches a schema keyword."""
    schema = {"type": "object", "properties": {"$ref": {"type": "string"}}, "default": {"$ref": "value"}}
    assert _parser()._normalize_schema(schema) == schema


def test_reference_expansion_budget_enforced() -> None:
    """Small acyclic documents cannot expand references into unbounded normalized trees."""
    parser = _parser()
    definitions: dict[str, Any] = {"leaf": {"type": "string"}}
    previous = "leaf"
    for index in range(7):
        name = f"level{index}"
        definitions[name] = {
            "type": "object",
            "properties": {str(child): {"$ref": f"#/definitions/{previous}"} for child in range(4)},
        }
        previous = name
    parser._raw_doc = {"definitions": definitions}
    with pytest.raises(CompileError, match="expansion"):
        parser._normalize_schema({"$ref": f"#/definitions/{previous}"})


def test_success_response_native_schema_uses_available_json() -> None:
    """Empty/non-JSON successes do not hide a later successful JSON response contract."""
    schema = {"type": "array", "items": {"type": ["integer", "null"], "minimum": 1}, "maxItems": 5}
    response = _parser()._success_response_schema(
        {
            "200": {"content": {"text/plain": {"schema": {"type": "string"}}}},
            "201": {"content": {"application/vnd.test+json": {"schema": schema}}},
            "400": {"schema": {"type": "boolean"}},
        }
    )
    assert response == schema


def test_request_vendor_json_without_content_type_support_rejected() -> None:
    """Vendor JSON bodies cannot be silently sent with an application/json content type."""
    with pytest.raises(CompileError, match="application/json"):
        _parser()._parse_request_body({"content": {"application/vnd.test+json": {"schema": {"type": "object"}}}})


def test_unsupported_response_schema_is_explicitly_unconstrained() -> None:
    """Unsupported response-only semantics yield no false claim of complete validation."""
    assert _parser()._success_response_schema({"200": {"schema": {"oneOf": [{"type": "string"}]}}}) == {}


def test_sdk_nullable_array_item_annotations_preserved() -> None:
    """SDK annotations preserve array items instead of flattening native nullable unions."""
    schema = {"type": ["array", "null"], "items": {"type": "integer"}}
    parameter = ParamSchema(name="values", location="query", param_type="array", required=True, json_schema=schema)
    assert _build_param_annotation(parameter) == "list[int] | None"
    endpoint = EndpointSpec(path="/x", method="GET", operation_id="get_x", summary="", response_json_schema=schema)
    assert _build_return_type(endpoint) == "list[int] | None"


def test_sdk_repeated_query_arrays_match_broker(gryphon_config: GryphonConfig) -> None:
    """Both SDK and broker represent scalar query arrays using repeated keys, never CSV."""
    endpoint = _parser()._parse_operation(
        "/x",
        "GET",
        {
            "parameters": [
                {"name": "ids", "in": "query", "schema": {"type": "array", "items": {"type": "integer"}}},
            ]
        },
        [],
    )
    assert endpoint is not None
    spec = ServerSpec(
        name="test",
        description="",
        base_url="https://example.com",
        swagger_hash="hash",
        is_read_only=True,
        endpoints=[endpoint],
    )
    code = CodeGenerator().generate(spec)
    assert "urllib.parse.urlencode(pairs)" in code
    assert "join(str" not in code
    manifest = Orchestrator(gryphon_config)._build_manifest("test", spec, "hash").endpoints[0]
    assert encode_request(manifest, manifest.base_url, {"ids": [1, 2]})[0] == "https://example.com/x?ids=1&ids=2"


def test_sdk_same_primary_operation_destination_uses_environment(sample_server_spec: ServerSpec) -> None:
    """An operation repeating its primary server URL needs no second environment mapping."""
    sample_server_spec.endpoints[0].base_url = sample_server_spec.base_url
    code = CodeGenerator().generate(sample_server_spec)
    assert sample_server_spec.base_url not in code


@pytest.mark.parametrize("path", ["/x%2fy", "/x%20y", "/x%252fy", "/../x", "//other/x"])
def test_catalog_ambiguous_paths_rejected(path: str) -> None:
    """Percent escapes and traversal cannot change the manifest's routing authority."""
    with pytest.raises(CompileError):
        validate_endpoint(EndpointSpec(path=path, method="GET", operation_id="get_x", summary=""))


async def test_native_schema_manifest_roundtrip_and_broker_validation(
    tmp_path: Path,
    gryphon_config: GryphonConfig,
    weather_swagger_source: SwaggerSource,
) -> None:
    """Compiler output is complete broker authority without importing generated SDK modules."""
    compiler = Orchestrator(gryphon_config)
    await compiler._compile_source(weather_swagger_source, False)
    registry = Registry(gryphon_config.compiled_output_dir)
    registry.load()
    endpoint = registry.get_endpoint("weather", "get_current_weather")
    assert endpoint.output_schema["properties"]["temperature"]["type"] == "number"
    assert endpoint.parameters[0].json_schema["type"] == "string"
    assert validate_arguments(endpoint, {"city": "London"}) == {"city": "London"}
    assert "city=London" in encode_request(endpoint, endpoint.base_url, {"city": "London"})[0]


@pytest.mark.parametrize("mutation", ["missing_output", "missing_native", "unsupported_output", "input_mismatch"])
async def test_manifest_incomplete_or_unsupported_native_contract_rejected(
    gryphon_config: GryphonConfig,
    weather_swagger_source: SwaggerSource,
    mutation: str,
) -> None:
    """Incompatible v2 catalogs require recompilation rather than accepting lossy model defaults."""
    compiler = Orchestrator(gryphon_config)
    await compiler._compile_source(weather_swagger_source, False)
    path = compiler._output_dir / "weather" / "manifest.json"
    raw = json.loads(path.read_text())
    endpoint = raw["endpoints"][0]
    if mutation == "missing_output":
        endpoint.pop("output_schema")
    elif mutation == "missing_native":
        endpoint["parameters"][0].pop("json_schema")
    elif mutation == "unsupported_output":
        endpoint["output_schema"] = {"type": "string", "pattern": "value"}
    else:
        endpoint["input_schema"]["properties"]["city"]["maxLength"] = 3
    path.write_text(json.dumps(raw))
    with pytest.raises(CompileError, match="compile again"):
        load_manifest(path)


def test_native_schema_validates_bounds_and_items() -> None:
    """Input validation rejects out-of-range numbers and wrong array item types."""
    endpoint = _parser()._parse_operation(
        "/x",
        "GET",
        {
            "parameters": [
                {"name": "latitude", "in": "query", "schema": {"type": "number", "minimum": -90, "maximum": 90}},
                {"name": "ids", "in": "query", "schema": {"type": "array", "items": {"type": "integer"}}},
            ]
        },
        [],
    )
    assert endpoint is not None
    validator = Draft202012Validator(input_schema(endpoint))
    assert validator.is_valid({"latitude": 51.5, "ids": [1, 2]})
    assert not validator.is_valid({"latitude": 91})
    assert not validator.is_valid({"ids": ["1"]})


@pytest.mark.parametrize("collection", [None, "csv", "ssv", "pipes"])
def test_swagger_non_multi_query_arrays_rejected(collection: str | None) -> None:
    """Swagger arrays are not advertised when they require unsupported delimited encoding."""
    parser = _parser()
    parser._raw_doc = {"swagger": "2.0"}
    raw: dict[str, Any] = {"name": "ids", "in": "query", "type": "array", "items": {"type": "integer"}}
    if collection is not None:
        raw["collectionFormat"] = collection
    with pytest.raises(CompileError, match="collectionFormat"):
        parser._parse_parameters([raw])
