"""Shared v2 catalog validation and native broker schema construction."""

from __future__ import annotations

import json
import keyword
import re
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import httpx
from jsonschema import Draft202012Validator

from gryphon.compiler.schemas import check_schema
from gryphon.errors import CompileError
from gryphon.models import EndpointManifest, EndpointSpec, ParamSchema, ServerManifest
from gryphon.security.form_encoding import check_form_schema
from gryphon.security.schema import validate_contract

if TYPE_CHECKING:
    from pathlib import Path

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")
_TYPES = frozenset({"string", "integer", "number", "boolean", "object", "array", "null"})
_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024


def module_name(name: str) -> str:
    """Normalize a configured display name, rejecting unsafe path characters."""
    if not name or not re.fullmatch(r"[A-Za-z0-9_ -]+", name):
        raise CompileError("Malformed server name; use letters, digits, spaces, underscores or hyphens")
    normalized = re.sub(r"_+", "_", re.sub(r"[^a-zA-Z0-9_]", "_", name)).strip("_").lower()
    if normalized and normalized[0].isdigit():
        normalized = f"m_{normalized}"
    validate_identifier(normalized)
    return normalized


def validate_identifier(name: str) -> None:
    """Require a canonical non-keyword catalog identifier."""
    if not _IDENTIFIER.fullmatch(name) or keyword.iskeyword(name):
        raise CompileError("Malformed catalog identifier")


def validate_base_url(value: str) -> str:
    """Reject credential-bearing, relative or non-HTTP base URLs without reflecting input."""
    try:
        url = httpx.URL(value)
        invalid = (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or any(char.isspace() for char in value)
            or any(char in value for char in "{}\\")
        )
        if invalid:
            raise ValueError("invalid base URL")
    except (ValueError, httpx.InvalidURL):
        raise CompileError("Base URL must be absolute HTTP(S), without credentials, query or fragment") from None
    return value.rstrip("/")


def contained_path(root: Path, *parts: str) -> Path:
    """Return a contained path, rejecting symlinks at every existing component."""
    candidate = root.joinpath(*parts).absolute()
    for node in (candidate, *candidate.parents):
        if node.is_symlink():
            raise CompileError("Symlink paths are not permitted for compiled artifacts")
    if not candidate.resolve().is_relative_to(root.absolute().resolve()):
        raise CompileError("Compiled artifact path escapes its root")
    return candidate


def parse_scalar(value: str, param_type: str) -> Any:
    """Decode legacy serialized defaults/enums using their declared JSON type."""
    if param_type == "string":
        return value
    try:
        result = json.loads(value.lower() if param_type == "boolean" else value)
        if param_type not in _TYPES or not Draft202012Validator({"type": param_type}).is_valid(result):
            raise ValueError("type mismatch")
        return result
    except (ValueError, TypeError):
        raise CompileError("Parameter default or enum does not match its declared type") from None


def parameter_schema(param: ParamSchema) -> dict[str, Any]:
    """Preserve native parameter schemas, falling back only for old simple fixture metadata."""
    if param.param_type not in _TYPES:
        raise CompileError("Unsupported parameter type")
    if param.json_schema:
        schema = deepcopy(param.json_schema)
    else:
        schema = {"type": param.param_type}
        if param.enum is not None:
            schema["enum"] = [parse_scalar(value, param.param_type) for value in param.enum]
        if param.default is not None:
            schema["default"] = parse_scalar(param.default, param.param_type)
    if param.description and "description" not in schema:
        schema["description"] = param.description
    check_schema(schema)
    if "default" in schema:
        try:
            validate_contract(schema["default"], schema)
        except ValueError:
            raise CompileError("Parameter default does not satisfy its JSON schema") from None
    return schema


def input_schema(endpoint: EndpointSpec | EndpointManifest) -> dict[str, Any]:
    """Build a closed broker input object with original wire names and native constraints."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in endpoint.parameters:
        if not param.name or param.name in properties or param.location not in {"path", "query", "header", "body"}:
            raise CompileError("Duplicate, empty or unsupported parameter name/location")
        schema = parameter_schema(param)
        if param.location == "body":
            if param.name != "json_body" or endpoint.request_body_schema is None:
                raise CompileError("Body parameters must use json_body and a request body schema")
            schema = deepcopy(endpoint.request_body_schema)
        properties[param.name] = schema
        if param.required or param.location == "path":
            required.append(param.name)
    if endpoint.request_body_schema is not None and "json_body" not in properties:
        properties["json_body"] = deepcopy(endpoint.request_body_schema)
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    check_schema(result)
    return result


def validate_endpoint(endpoint: EndpointSpec | EndpointManifest) -> None:
    """Validate routing and reject ambiguous wire-to-SDK name mappings."""
    from gryphon.compiler.codegen import _safe_name

    name = endpoint.operation_id if isinstance(endpoint, EndpointSpec) else endpoint.function_name
    validate_identifier(name)
    if endpoint.method not in _METHODS:
        raise CompileError("Unsupported HTTP method")
    path = endpoint.path
    if not path.startswith("/") or path.startswith("//") or any(c in path for c in "\\?#\r\n"):
        raise CompileError("Endpoint path must be a relative absolute-path template")
    if any(part in {".", ".."} for part in path.split("/")) or "%" in path:
        raise CompileError("Endpoint path traversal or encoded separators are not permitted")
    placeholders = re.findall(r"\{([^{}]+)\}", path)
    if "{" in re.sub(r"\{[^{}]+\}", "", path) or "}" in re.sub(r"\{[^{}]+\}", "", path):
        raise CompileError("Malformed endpoint path template")
    if set(placeholders) != {p.name for p in endpoint.parameters if p.location == "path"}:
        raise CompileError("Path template and declared path parameters disagree")
    aliases = [_safe_name(param.name) for param in endpoint.parameters]
    if len(set(aliases)) != len(aliases):
        raise CompileError("Parameter names collide after Python normalization")
    if any(alias in {"urllib", "httpx", "json", "os"} for alias in [name, *aliases]):
        raise CompileError("Name collides with a generated SDK import")
    if any(p.name == "json_body" and p.location != "body" for p in endpoint.parameters):
        raise CompileError("json_body is reserved for request bodies")
    if endpoint.read_only_post and endpoint.method != "POST":
        raise CompileError("Read-only POST classification requires POST")
    if endpoint.request_body_media_type != "application/json":
        try:
            check_form_schema(
                endpoint.request_body_schema or {}, multipart=endpoint.request_body_media_type == "multipart/form-data"
            )
        except ValueError:
            raise CompileError("Invalid form body contract") from None
    input_schema(endpoint)
    if isinstance(endpoint, EndpointManifest) and endpoint.mcp_binding is not None:
        _validate_mcp_endpoint(endpoint)
    output = endpoint.response_json_schema if isinstance(endpoint, EndpointSpec) else endpoint.output_schema
    check_schema(output)


def _validate_mcp_endpoint(endpoint: EndpointManifest) -> None:
    """Reject forged or inconsistent transport metadata before granting broker dispatch authority."""
    binding = endpoint.mcp_binding
    assert binding is not None
    validate_base_url(binding.endpoint)
    if (
        endpoint.method != "POST"
        or endpoint.read_only_post
        or endpoint.request_body_media_type != "application/json"
        or endpoint.request_body_schema is None
        or endpoint.request_body_schema.get("type") != "object"
        or endpoint.path != f"/__mcp__/{endpoint.function_name}"
        or endpoint.base_url.rstrip("/") != binding.endpoint.rstrip("/")
        or len(endpoint.parameters) != 1
        or endpoint.parameters[0].name != "json_body"
        or endpoint.parameters[0].location != "body"
        or not endpoint.parameters[0].required
    ):
        raise CompileError("MCP binding requires a fixed POST endpoint and required JSON object arguments")


def load_manifest(path: Path) -> ServerManifest:
    """Load complete v2 metadata, or require recompilation without leaking raw values."""
    try:
        with path.open("rb") as stream:
            raw_bytes = stream.read(_MAX_MANIFEST_BYTES + 1)
        if len(raw_bytes) > _MAX_MANIFEST_BYTES:
            raise ValueError("manifest exceeds size limit")
        raw = json.loads(raw_bytes)
        if not isinstance(raw, dict) or raw.get("format_version") != 2:
            raise ValueError("legacy format")
        _check_manifest_fields(raw)
        manifest = ServerManifest.model_validate(raw)
        validate_identifier(manifest.server_name)
        validate_base_url(manifest.base_url)
        names: set[str] = set()
        for endpoint in manifest.endpoints:
            validate_endpoint(endpoint)
            validate_base_url(endpoint.base_url)
            if endpoint.function_name in names or endpoint.input_schema != input_schema(endpoint):
                raise ValueError("duplicate endpoint or inconsistent input schema")
            if (
                manifest.is_read_only
                and endpoint.method not in {"GET", "HEAD", "OPTIONS"}
                and not endpoint.read_only_post
            ):
                raise ValueError("read-only manifest contains writes")
            names.add(endpoint.function_name)
        return manifest
    except (OSError, ValueError, TypeError, CompileError, RecursionError):
        raise CompileError("Incompatible or invalid manifest; run gryphon compile again") from None


def _check_manifest_fields(raw: dict[str, Any]) -> None:
    """Reject v2 metadata predating native schemas rather than filling lossy defaults."""
    expected = {"parameters", "response_fields", "request_body_schema", "base_url", "input_schema", "output_schema"}
    for endpoint in raw.get("endpoints", []):
        if not isinstance(endpoint, dict) or not expected <= endpoint.keys():
            raise ValueError("incomplete endpoint metadata")
        for parameter in endpoint["parameters"]:
            if not isinstance(parameter, dict) or "json_schema" not in parameter:
                raise ValueError("incomplete native parameter schema")
