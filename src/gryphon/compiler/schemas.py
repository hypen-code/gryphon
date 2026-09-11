"""Bounded OpenAPI 3.0/3.1 and Swagger 2.0 schema normalization for v2 contracts.

Supported wire styles are simple scalar path/header and form scalar/repeated-key
query parameters (explode=true for arrays; Swagger collectionFormat=multi).
Object, delimited-array, cookie, content and allowReserved encodings are rejected.
JSON bodies/responses support bounded scalar/object/array constraints and nullable
types, not composition, regex, conditionals, external/chained/recursive references
or other dialect semantics. Format/readOnly/writeOnly remain annotations.
"""

from __future__ import annotations

import json
from typing import Any

from gryphon.errors import CompileError
from gryphon.models import ParamSchema, ResponseField
from gryphon.security.schema import check_schema as check_bounded_schema
from gryphon.utils.logging import get_logger

logger = get_logger(__name__)

# Maximum nesting depth before we skip a schema
_MAX_SCHEMA_DEPTH = 2
_MAX_INPUT_DEPTH = 16
_MAX_SCHEMA_NODES = 10000

# Unsupported discriminator keywords
_COMPLEX_KEYWORDS = {"oneOf", "anyOf", "allOf", "discriminator", "not"}
_ALLOWED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "$ref",
        "nullable",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "title",
        "description",
        "default",
        "example",
        "examples",
        "format",
        "readOnly",
        "writeOnly",
        "deprecated",
    }
)
_ANNOTATIONS = frozenset({"description", "title", "default", "example", "examples", "format", "deprecated"})
_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean", "null"})


def _encode_value(value: Any) -> str:
    """Serialize parameter summary metadata without interpreting Python expressions."""
    return value if isinstance(value, str) else json.dumps(value, allow_nan=False)


def check_schema(schema: dict[str, Any]) -> None:
    """Require the same bounded normalized schema contract as broker validation.

    Args:
        schema: Local normalized JSON Schema with no unresolved references.

    Raises:
        CompileError: If schema semantics or structural bounds are unsupported.
    """
    try:
        check_bounded_schema(schema)
    except (ValueError, TypeError, RecursionError):
        raise CompileError("Invalid or unsupported normalized JSON schema") from None


class SchemaParser:
    """Normalize request schemas and preserve full supported successful response schemas."""

    _raw_doc: dict[str, Any]
    _normalization_nodes: int

    def _resolve_ref(self, ref: str) -> dict[str, Any] | None:
        """Resolve a local RFC6901 pointer without fetching external schema documents."""
        if not isinstance(ref, str) or not ref.startswith("#/"):
            logger.warning("unsupported_schema", reason="external reference")
            return None  # External $ref not supported
        node: Any = self._raw_doc
        try:
            for part in ref[2:].split("/"):
                node = node[part.replace("~1", "/").replace("~0", "~")]
            return dict(node) if isinstance(node, dict) else None
        except (KeyError, TypeError):
            return None

    def _normalize_schema(
        self,
        schema: dict[str, Any],
        depth: int = 0,
        refs: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Resolve bounded local schemas and fail explicitly on unsupported validation semantics."""
        if depth == 0:
            validate_document_tree(schema)
            self._normalization_nodes = 0
        self._normalization_nodes += 1
        if self._normalization_nodes > _MAX_SCHEMA_NODES or depth > _MAX_INPUT_DEPTH:
            raise CompileError("Unsupported schema nesting or reference expansion size")
        if not isinstance(schema, dict) or any(not isinstance(key, str) for key in schema):
            raise CompileError("Unsupported schema shape")
        if any(key not in _ALLOWED_KEYWORDS and not key.startswith("x-") for key in schema):
            raise CompileError("Unsupported schema composition or dialect keyword")
        if "$ref" in schema:
            ref = schema["$ref"]
            if not isinstance(ref, str) or ref in refs:
                raise CompileError("Unsupported recursive schema reference")
            resolved = self._resolve_ref(ref)
            if resolved is None or "$ref" in resolved:
                raise CompileError("Unsupported unresolved, external or chained schema reference")
            if set(schema) - _ANNOTATIONS - {"$ref"}:
                raise CompileError("Unsupported validation siblings beside a schema reference")
            combined = {**resolved, **{key: value for key, value in schema.items() if key != "$ref"}}
            result = self._normalize_schema(combined, depth + 1, (*refs, ref))
        else:
            result = self._normalize_children(schema, depth, refs)
        if depth == 0:
            check_schema(result)
        return result

    def _normalize_children(self, schema: dict[str, Any], depth: int, refs: tuple[str, ...]) -> dict[str, Any]:
        """Normalize supported child schemas, OpenAPI nullable types and exclusive bounds."""
        result = {key: value for key, value in schema.items() if not key.startswith("x-")}
        nullable = result.pop("nullable", False)
        if not isinstance(nullable, bool):
            raise CompileError("Nullable must be a boolean")
        if nullable:
            kind = result.get("type")
            if not isinstance(kind, (str, list)):
                raise CompileError("Nullable schemas must declare a JSON type")
            result["type"] = list(dict.fromkeys([kind, "null"] if isinstance(kind, str) else [*kind, "null"]))
        if "properties" in result:
            properties = result["properties"]
            if not isinstance(properties, dict):
                raise CompileError("Invalid schema properties")
            result["properties"] = {
                key: self._normalize_schema(value, depth + 1, refs) for key, value in properties.items()
            }
        for key in ("items", "additionalProperties"):
            if isinstance(result.get(key), dict):
                result[key] = self._normalize_schema(result[key], depth + 1, refs)
        for keyword, bound in (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")):
            if isinstance(result.get(keyword), bool):
                enabled = result.pop(keyword)
                if enabled:
                    if bound not in result:
                        raise CompileError("Exclusive bound requires a numeric bound")
                    result[keyword] = result.pop(bound)
        return result

    def _parse_parameters(self, raw_params: list[Any]) -> list[ParamSchema]:
        """Merge path/operation declarations by original wire name and location."""
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in raw_params:
            if not isinstance(raw, dict):
                raise CompileError("Invalid parameter object")
            # Resolve $ref at top level
            if "$ref" in raw:
                raw = self._resolve_ref(raw["$ref"]) or {}  # noqa: PLW2901
            if not isinstance(raw.get("name"), str) or not raw["name"]:
                raise CompileError("Unsupported parameter name")
            if raw.get("in") not in {"path", "query", "header", "body"}:
                raise CompileError("Unsupported parameter location")
            merged[(raw["name"], raw["in"])] = raw
        return [self._parameter(raw) for raw in merged.values()]

    def _parameter(self, raw: dict[str, Any]) -> ParamSchema:
        """Preserve native constraints while rejecting unsupported wire serialization."""
        if "content" in raw or raw.get("allowReserved"):
            raise CompileError("Unsupported parameter content or reserved serialization")
        location = raw["in"]
        expected_style = "form" if location == "query" else "simple"
        if raw.get("style", expected_style) != expected_style:
            raise CompileError("Unsupported parameter serialization style")
        for flag in ("explode", "required", "allowReserved", "allowEmptyValue"):
            if flag in raw and not isinstance(raw[flag], bool):
                raise CompileError("Parameter flags must be booleans")
        parameter_fields = {
            "name",
            "in",
            "required",
            "style",
            "explode",
            "collectionFormat",
            "allowEmptyValue",
            "allowReserved",
        }
        legacy = {key: value for key, value in raw.items() if key not in parameter_fields}
        schema = self._normalize_schema(raw.get("schema", legacy))
        self._validate_wire_schema(schema, location, raw)
        return ParamSchema(
            name="json_body" if location == "body" else raw["name"],
            location=location,
            param_type=self._extract_type(schema),
            required=location == "path" or bool(raw.get("required", False)),
            description=str(raw.get("description", "")),
            json_schema=schema,
            default=_encode_value(schema["default"]) if "default" in schema else None,
            enum=[_encode_value(value) for value in schema["enum"]] if "enum" in schema else None,
        )

    def _validate_wire_schema(self, schema: dict[str, Any], location: str, raw: dict[str, Any]) -> None:
        """Permit scalar parameters and repeated scalar query arrays, not ambiguous object encodings."""
        if location == "body":
            return
        kind = schema.get("type")
        kinds = set(kind) if isinstance(kind, list) else {kind}
        if kinds <= _SCALAR_TYPES:
            return
        if kinds - {"null"} != {"array"} or location != "query" or raw.get("explode") is False:
            raise CompileError("Unsupported parameter encoding; only scalars or repeated query arrays are supported")
        if self._raw_doc.get("swagger") == "2.0" and raw.get("collectionFormat", "csv") != "multi":
            raise CompileError("Unsupported Swagger array collectionFormat; use multi")
        items = schema.get("items", {})
        item_type = items.get("type") if isinstance(items, dict) else None
        item_types = set(item_type) if isinstance(item_type, list) else {item_type}
        if not item_types <= _SCALAR_TYPES:
            raise CompileError("Query arrays must declare scalar item types")

    def _parse_request_body(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """Return an application/json body schema, rejecting encodings the broker cannot send."""
        if not body:
            return None
        if "$ref" in body:
            body = self._resolve_ref(body["$ref"]) or {}
        media: dict[str, Any] = body.get("content", {}).get("application/json", {})
        schema = media.get("schema")
        if not isinstance(schema, dict):
            raise CompileError("Unsupported request media type or missing application/json schema")
        # Check complexity — reject unsupported request validation semantics.
        return self._normalize_schema(schema)

    @staticmethod
    def _media_schema(content: dict[str, Any]) -> dict[str, Any]:
        """Select application/json or a structured +json response without external documents."""
        for name in sorted(content, key=lambda value: (value != "application/json", value)):
            if name == "application/json" or name.startswith("application/") and name.endswith("+json"):
                media = content[name]
                if isinstance(media, dict) and isinstance(media.get("schema"), dict):
                    return dict(media["schema"])
        raise CompileError("Unsupported media type or missing JSON schema")

    def _success_response_schema(self, responses: dict[str, Any]) -> dict[str, Any]:
        """Preserve the first supported successful JSON schema, ignoring errors and empty responses."""
        for status in sorted(responses, key=str):
            if str(status).startswith("2") and isinstance(responses[status], dict):
                schema = self._response_json_schema(responses[status])
                if schema:
                    return schema
        return {}

    def _response_json_schema(self, response: dict[str, Any]) -> dict[str, Any]:
        """Normalize a JSON response, omitting unsupported shapes with a diagnostic."""
        if "$ref" in response:
            response = self._resolve_ref(response["$ref"]) or {}
        # Prefer application/json; support structured +json without guessing non-JSON response contracts.
        try:
            if "schema" in response:
                raw = response["schema"]
            elif response.get("content"):
                raw = self._media_schema(response["content"])
            else:
                return {}
            return self._normalize_schema(raw)
        except CompileError:
            logger.warning("unsupported_response_schema", reason="unsupported media, composition or reference")
            return {}

    def _parse_response_schema(self, responses: dict[str, Any]) -> list[ResponseField]:
        """Return bounded display fields while full JSON metadata is preserved separately."""
        return self._schema_to_fields(self._success_response_schema(responses), 0)

    def _extract_response_fields(self, response: dict[str, Any]) -> list[ResponseField]:
        """Summarize fields from one supported successful response."""
        return self._schema_to_fields(self._response_json_schema(response), 0)

    def _schema_to_fields(self, schema: dict[str, Any], depth: int) -> list[ResponseField]:
        """Build display fields with at most one nested object level."""
        if depth > _MAX_SCHEMA_DEPTH or not isinstance(schema, dict):
            return []
        if self._extract_type(schema) == "array":
            items = schema.get("items", {})
            if isinstance(items, dict) and "$ref" in items:
                items = self._resolve_ref(items["$ref"]) or {}
            nested = self._schema_to_fields(items, depth + 1) if isinstance(items, dict) else []
            return [ResponseField(name="items", field_type="array", nested=nested or None)]
        fields: list[ResponseField] = []
        for name, prop in schema.get("properties", {}).items():
            if "$ref" in prop:
                prop = self._resolve_ref(prop["$ref"]) or {}  # noqa: PLW2901
            nested = self._schema_to_fields(prop, depth + 1) if depth < 1 else []
            fields.append(
                ResponseField(
                    name=name,
                    field_type=self._extract_type(prop),
                    description=str(prop.get("description", "")),
                    required=name in schema.get("required", []),
                    nested=nested or None,
                )
            )
        return fields

    def _extract_type(self, schema: dict[str, Any]) -> str:
        """Return a primary display type without altering native JSON-schema types."""
        if not isinstance(schema, dict):
            return "string"
        kind = schema.get("type", "object" if "properties" in schema else "string")
        if isinstance(kind, list):
            # Handle nullable types like ["string", "null"]
            return str(next((item for item in kind if item != "null"), "null"))
        return str(kind)


def validate_document_tree(document: Any) -> None:
    """Bound alias expansion, cycles and nesting before schema processing.

    Args:
        document: Parsed YAML/JSON tree, before resolving local references.

    Raises:
        CompileError: If document size, depth or mapping keys are unsupported.
    """
    stack: list[tuple[Any, int]] = [(document, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count + len(stack) > _MAX_SCHEMA_NODES or depth > _MAX_INPUT_DEPTH * 2:
            raise CompileError("Document structure exceeds supported size/depth")
        if isinstance(node, dict):
            if any(not isinstance(key, (str, int)) for key in node):
                raise CompileError("Invalid document mapping key")
            stack.extend((value, depth + 1) for value in node.values())
        elif isinstance(node, list):
            stack.extend((value, depth + 1) for value in node)
