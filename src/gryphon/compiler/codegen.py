"""Deterministic SDK generation; source is never a host tool implementation."""

from __future__ import annotations

import ast
import keyword
import re
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from gryphon.compiler.catalog import module_name, parse_scalar, validate_endpoint
from gryphon.errors import CompileError
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from gryphon.models import EndpointSpec, ParamSchema, ServerSpec

logger = get_logger(__name__)

# Directory containing Jinja2 templates
_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Type mapping from swagger/JSON schema types to Python type annotations
_TYPE_MAP = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "object": "dict[str, Any]",
    "array": "list[Any]",
    "null": "None",
}


def _wrap_text(text: str, width: int, subsequent_indent: str = "") -> str:
    """Wrap metadata paragraphs without interpreting them as Python source."""
    return "\n\n".join(
        textwrap.fill(paragraph.replace("\n", " ").strip(), width=width, subsequent_indent=subsequent_indent)
        for paragraph in text.split("\n\n")
    )


def _swagger_type_to_python(swagger_type: str) -> str:
    """Return a known annotation or Any for an unspecified JSON shape."""
    return _TYPE_MAP.get(swagger_type, "Any")


def _schema_annotation(schema: dict[str, Any], fallback: str = "Any") -> str:
    """Describe supported native scalar unions and array items without source interpolation."""
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(dict.fromkeys(_schema_annotation({**schema, "type": value}) for value in kind))
    if kind == "array" and isinstance(schema.get("items"), dict):
        return f"list[{_schema_annotation(schema['items'])}]"
    return _swagger_type_to_python(str(kind)) if kind is not None else fallback


def _build_param_annotation(param: ParamSchema) -> str:
    """Preserve native nullability and item types in compact SDK annotations."""
    base = _schema_annotation(param.json_schema, _swagger_type_to_python(param.param_type))
    if param.required or "None" in base.split(" | "):
        return base
    return f"{base} | None"


def _signature_parts(endpoint: EndpointSpec) -> list[str]:
    """Build required-first SDK declarations with literal-safe native defaults."""
    parts: list[str] = []
    # Required params first
    for param in endpoint.parameters:
        if param.required:
            parts.append(f"{_safe_name(param.name)}: {_build_param_annotation(param)}")
    # Optional params with defaults
    for param in endpoint.parameters:
        if not param.required:
            if param.json_schema:
                value = param.json_schema.get("default")
            else:
                value = parse_scalar(param.default, param.param_type) if param.default is not None else None
            if isinstance(value, (list, dict)):
                value = None
            parts.append(f"{_safe_name(param.name)}: {_build_param_annotation(param)} = {value!r}")
    # Request body as json_body for mutating methods
    if endpoint.request_body_schema is not None and not any(p.location == "body" for p in endpoint.parameters):
        parts.append("json_body: dict[str, Any] | None = None")
    return parts


def _build_function_signature(endpoint: EndpointSpec) -> str:
    """Join separate parameter declarations without parsing annotation text."""
    return ", ".join(_signature_parts(endpoint))


def _build_params_dict(endpoint: EndpointSpec) -> str:
    """Build a query mapping with original wire names encoded as string literals."""
    params = [param for param in endpoint.parameters if param.location == "query"]
    if not params:
        return "None"
    return "{\n" + "\n".join(f"        {p.name!r}: {_safe_name(p.name)}," for p in params) + "\n    }"


def _build_path_formatted(endpoint: EndpointSpec) -> str:
    """Build encoded literal path replacements, never executable f-string expressions."""
    result = repr(endpoint.path)
    for param in endpoint.parameters:
        if param.location == "path":
            placeholder = repr("{" + param.name + "}")
            result += f".replace({placeholder}, urllib.parse.quote(str({_safe_name(param.name)}), safe=''))"
    return result


def _safe_name(name: str) -> str:
    """Normalize SDK spelling while retaining original wire names in manifests."""
    # Split camelCase/PascalCase boundaries before lowercasing
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", name)
    sanitized = re.sub(r"_+", "_", re.sub(r"[^a-zA-Z0-9_]", "_", name)).strip("_").lower()
    if sanitized and sanitized[0].isdigit():
        sanitized = f"p_{sanitized}"
    if keyword.iskeyword(sanitized):
        sanitized = f"{sanitized}_"
    return sanitized or "param"


def _safe_field_name(name: str) -> str:
    """Return a safe legacy SDK field identifier without interpolating source fragments."""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if not name or name[0].isdigit():
        return f"f_{name}"
    return f"{name}_" if keyword.iskeyword(name) else name


def _to_pascal_case(snake: str) -> str:
    """Build deterministic response declaration names from sanitized identifiers."""
    return "".join(word.capitalize() for word in _safe_name(snake).split("_"))


def _typed_dict(name: str, fields: list[tuple[str, str]]) -> str:
    """Use functional TypedDict syntax whenever JSON keys are not Python identifiers."""
    if all(key.isidentifier() and not keyword.iskeyword(key) for key, _ in fields):
        return f"class {name}(TypedDict, total=False):\n" + "\n".join(f"    {key}: {kind}" for key, kind in fields)
    mapping = ", ".join(f"{key!r}: {kind}" for key, kind in fields)
    return f"{name} = TypedDict({name!r}, {{{mapping}}}, total=False)"


def _build_typeddict_classes(endpoint: EndpointSpec) -> list[str]:
    """Render response declarations without interpolating untrusted field names as source."""
    schema = endpoint.response_schema
    if not schema:
        return []
    pascal = _to_pascal_case(endpoint.operation_id)
    classes: list[str] = []
    # Detect array-of-items: single "items" field with nested structure
    is_array = len(schema) == 1 and schema[0].name == "items" and schema[0].nested
    fields = schema[0].nested if is_array else schema
    base_name = f"{pascal}ResponseItem" if is_array else f"{pascal}Response"
    if not fields:
        return []
    nested_names: dict[str, str] = {}
    # Emit nested TypedDicts first (dependencies before parent)
    for index, field in enumerate(fields):
        if field.field_type == "object" and field.nested:
            nested_name = f"{base_name}{_to_pascal_case(field.name)}"
            if nested_name in nested_names.values():
                nested_name += str(index)
            nested_names[field.name] = nested_name
            classes.append(
                _typed_dict(nested_name, [(f.name, _swagger_type_to_python(f.field_type)) for f in field.nested])
            )
    # Emit the main TypedDict
    declarations = [
        (field.name, nested_names.get(field.name, _swagger_type_to_python(field.field_type))) for field in fields
    ]
    classes.append(_typed_dict(base_name, declarations))
    return classes


def _build_return_type(endpoint: EndpointSpec) -> str:
    """Use native primitive/array response types, with detailed TypedDicts for object fields."""
    schema = endpoint.response_schema
    native = endpoint.response_json_schema
    if native.get("type") != "object" and native.get("type") != "array" and "type" in native:
        return _schema_annotation(native)
    if not schema:
        return _schema_annotation(native)
    pascal = _to_pascal_case(endpoint.operation_id)
    # Array response
    if len(schema) == 1 and schema[0].name == "items":
        return f"list[{pascal}ResponseItem]" if schema[0].nested else _schema_annotation(native, "list[Any]")
    # Object response with fields
    return f"{pascal}Response"


def _build_docstring_args(endpoint: EndpointSpec) -> list[dict[str, Any]]:
    """Describe SDK aliases and original broker parameter names explicitly."""
    args: list[dict[str, Any]] = []
    for param in endpoint.parameters:
        safe = _safe_name(param.name)
        # 120 - 8 (indent) - len(name) - 2 (": ") - 11 (" (required)")
        description = param.description or param.name
        if safe != param.name:
            description += f"; broker key: {param.name!r}"
        args.append(
            {
                "name": safe,
                "type": _build_param_annotation(param),
                "required": param.required,
                "description": _wrap_text(description, max(40, 99 - len(safe))),
            }
        )
    if endpoint.request_body_schema is not None and not any(p.location == "body" for p in endpoint.parameters):
        args.append(
            {
                "name": "json_body",
                "type": "dict[str, Any] | None",
                "required": False,
                "description": "Request body as JSON object",
            }
        )
    return args


def _function_docstring(endpoint: EndpointSpec) -> str:
    """Encode all documentation as a literal, preventing quote/newline injection."""
    text = endpoint.summary + "\n\n" + endpoint.description + "\n\nArgs:\n"
    for arg in _build_docstring_args(endpoint):
        text += f"    {arg['name']}: {arg['description']} ({'required' if arg['required'] else 'optional'})\n"
    text += "\nReturns:\n    " + (", ".join(field.name for field in endpoint.response_schema) or "API response data.")
    return ascii(text)


class CodeGenerator:
    """Generate SDK documentation modules, never host-side broker implementations."""

    def __init__(self) -> None:
        """Initialize strict templates with Python literal encoding."""
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._env.filters["pyrepr"] = ascii
        self._env.globals.update(swagger_type_to_python=_swagger_type_to_python, safe_name=_safe_name)

    def generate(self, spec: ServerSpec) -> str:
        """Render validated source and verify syntax without executing it."""
        module = module_name(spec.name)
        for endpoint in spec.endpoints:
            validate_endpoint(endpoint)
        if len({ep.operation_id for ep in spec.endpoints}) != len(spec.endpoints):
            raise CompileError("Duplicate generated function names")
        functions_data = [self._prepare_function_data(ep, spec.server_url_vars, spec.base_url) for ep in spec.endpoints]
        # Sanitize the server name for use in env var names: "cse-api" → "CSE_API".
        # This must match the canonical module name used by the broker and vault.
        # Names are validated before generating identifiers or directory paths.
        prefix = module.upper()
        # Build ordered list of extra server URL env vars for the template
        extra_server_vars = []
        for index, (_url, variable) in enumerate(spec.server_url_vars.items(), 1):
            if variable != f"_BASE_URL_{index}":
                raise CompileError("Invalid extra server variable mapping")
            extra_server_vars.append({"var_name": variable, "env_var": f"GRYPHON_{prefix}_{variable.lstrip('_')}"})
        try:
            code: str = self._env.get_template("function.py.j2").render(
                server_name=module,
                server_env_prefix=prefix,
                description=ascii(spec.description),
                functions=functions_data,
                extra_server_vars=extra_server_vars,
            )
            ast.parse(code)
        except (TemplateError, SyntaxError):
            raise CompileError("Generated SDK source failed validation") from None
        logger.debug("code_generated", server=module, functions=len(functions_data), code_size=len(code))
        return code

    def _prepare_function_data(
        self,
        endpoint: EndpointSpec,
        url_to_var: dict[str, str],
        primary_url: str = "",
    ) -> dict[str, Any]:
        """Build literal-safe SDK context for one endpoint."""
        # Prefer a module-level env-var-backed variable over a hardcoded URL string.
        # base_url_var: e.g. "_BASE_URL_1" — used in the template as a bare identifier.
        # base_url: unknown raw URL overrides are rejected rather than embedded.
        variable = url_to_var.get(endpoint.base_url, "") if endpoint.base_url else ""
        if endpoint.base_url and endpoint.base_url != primary_url and not variable:
            raise CompileError("Endpoint base URL must have an environment-backed SDK mapping")
        return {
            "name": endpoint.operation_id,
            "method": endpoint.method,
            "path_expr": _build_path_formatted(endpoint),
            "signature": _build_function_signature(endpoint),
            "params": _signature_parts(endpoint),
            "params_dict": _build_params_dict(endpoint),
            "has_body": endpoint.request_body_schema is not None,
            "docstring": _function_docstring(endpoint),
            "has_query_params": any(param.location == "query" for param in endpoint.parameters),
            "header_params": {p.name: _safe_name(p.name) for p in endpoint.parameters if p.location == "header"},
            "base_url_var": variable,  # module var name (environment-backed)
            "base_url": "",  # raw URL fallback is deliberately disabled
            "return_type": _build_return_type(endpoint),
            "typeddict_classes": _build_typeddict_classes(endpoint),
        }
