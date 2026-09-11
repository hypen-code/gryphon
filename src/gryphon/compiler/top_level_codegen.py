"""Optional SDK async wrappers; v2 never imports generated modules into the host server."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from gryphon.compiler.catalog import validate_endpoint, validate_identifier
from gryphon.compiler.codegen import _build_function_signature, _function_docstring, _safe_name, _signature_parts
from gryphon.errors import CompileError
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from gryphon.models import EndpointSpec, ServerSpec

logger = get_logger(__name__)
_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _normalize_function_name(raw: str) -> str:
    """Normalize configured SDK wrapper names to compiled operation identifiers."""
    # Mirror the same camelCase→snake_case logic used in swagger_parser._sanitize_identifier
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw)
    name = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", name)
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    return re.sub(r"_+", "_", name).strip("_").lower() or raw.lower()


class TopLevelFunctionGenerator:
    """Generate optional SDK wrappers, never host-side MCP implementations."""

    def __init__(self) -> None:
        """Initialize strict literal-safe SDK templates."""
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        self._env.filters["pyrepr"] = ascii

    def generate(self, spec: ServerSpec, module_name: str, top_level_names: list[str]) -> str | None:
        """Generate async SDK wrappers for selected names, or None if none resolve.

        Args:
            spec: Parsed endpoint metadata.
            module_name: Canonical Python module name.
            top_level_names: Configured names, optionally camelCase.

        Returns:
            Validated Python SDK source, never loaded by the v2 host server.
        """
        validate_identifier(module_name)
        for endpoint in spec.endpoints:
            validate_endpoint(endpoint)
        if not top_level_names:
            return None
        # Normalise requested names to the snake_case form the compiler uses
        requested = {_normalize_function_name(name) for name in top_level_names}
        # Match against compiled endpoints (operation_id is already snake_case)
        matched = [endpoint for endpoint in spec.endpoints if endpoint.operation_id in requested]
        # Report any unresolved names so the user can fix the YAML config
        if requested - {ep.operation_id for ep in matched}:
            logger.warning("top_level_functions_not_found", server=module_name)
        if not matched:
            return None
        try:
            code: str = self._env.get_template("top_level_functions.py.j2").render(
                server_name=module_name,
                module_name=module_name,
                functions=[self._prepare_function_data(endpoint) for endpoint in matched],
            )
            ast.parse(code)
        except (TemplateError, SyntaxError) as exc:
            raise CompileError("SDK async-wrapper template rendering failed") from exc
        logger.debug("top_level_functions_generated", server=module_name, count=len(matched))
        return code

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _prepare_function_data(self, endpoint: EndpointSpec) -> dict[str, Any]:
        """Build literal-safe Jinja context for one selected SDK wrapper."""
        # Build "kwarg=kwarg" strings so asyncio.to_thread gets the right values
        call_kwargs = [f"{_safe_name(param.name)}={_safe_name(param.name)}" for param in endpoint.parameters]
        if endpoint.request_body_schema is not None and not any(p.location == "body" for p in endpoint.parameters):
            call_kwargs.append("json_body=json_body")
        return {
            "name": endpoint.operation_id,
            "signature": _build_function_signature(endpoint),
            "params": _signature_parts(endpoint),
            "docstring": _function_docstring(endpoint),
            "call_kwargs": call_kwargs,
        }
