"""Immutable v2 broker catalog with deterministic, bounded lexical discovery."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from gryphon.compiler.catalog import contained_path, load_manifest, validate_identifier
from gryphon.errors import CompileError, FunctionNotFoundError, ServerNotFoundError
from gryphon.models import EndpointManifest, FunctionInfo, ParamSchema, ResponseField, ServerInfo, ServerManifest
from gryphon.utils.logging import get_logger

logger = get_logger(__name__)
_MAX_ENDPOINTS = 10000
_MAX_SERVERS = 1000
_MAX_INDEX_TEXT = 2048
_MAX_QUERY_CHARS = 2048
_MAX_SEARCH_RESULTS = 100
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_SOURCE_CACHE = 64


class Registry:
    """Load complete v2 manifests without importing generated source or reading credentials."""

    def __init__(self, compiled_dir: str) -> None:
        """Initialize an empty catalog at a trusted compiled output root."""
        self._compiled_dir = Path(compiled_dir)
        self._servers: dict[str, ServerManifest] = {}
        self._function_source_cache: dict[str, str] = {}
        self._search_index: dict[str, Counter[str]] = {}
        self._endpoints: dict[str, EndpointManifest] = {}
        self._fingerprint = hashlib.sha256(b"[]").hexdigest()

    def load(self) -> None:
        """Replace loaded metadata atomically, rejecting incompatible manifests explicitly."""
        self._servers.clear()
        self._endpoints.clear()
        self._search_index.clear()
        self._function_source_cache.clear()
        self._fingerprint = hashlib.sha256(b"[]").hexdigest()
        contained_path(self._compiled_dir)
        if not self._compiled_dir.exists():
            logger.warning("compiled_dir_not_found")
            return
        paths = sorted(self._compiled_dir.glob("*/manifest.json"))
        if len(paths) > _MAX_SERVERS:
            raise CompileError("Catalog exceeds the supported server limit")
        try:
            for path in paths:
                self._load_manifest(path)
            self._build_index()
        except (CompileError, OSError):  # Fail closed: never expose a partially compatible catalog.
            logger.error("manifest_load_failed", reason="run gryphon compile again")
            self._servers.clear()
            self._endpoints.clear()
            self._search_index.clear()
            raise
        serialized = [self._servers[name].model_dump(mode="json") for name in sorted(self._servers)]
        self._fingerprint = hashlib.sha256(
            json.dumps(serialized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        logger.info("registry_loaded", servers=len(self._servers), total_functions=len(self._endpoints))

    def _load_manifest(self, manifest_path: Path) -> None:
        """Validate a contained manifest and bind its identity to the actual directory."""
        path = contained_path(self._compiled_dir, manifest_path.parent.name, manifest_path.name)
        manifest = load_manifest(path)
        # Index by directory name (valid Python identifier) so SDK imports like
        # `from open_meteo_weather_api.functions import …` resolve correctly.
        name = path.parent.name
        validate_identifier(name)
        if manifest.server_name != name or name in self._servers:
            raise CompileError("Manifest server identity mismatch; run gryphon compile again")
        if sum(len(value.endpoints) for value in self._servers.values()) + len(manifest.endpoints) > _MAX_ENDPOINTS:
            raise CompileError("Catalog exceeds the supported endpoint limit")
        self._servers[name] = manifest

    def _build_index(self) -> None:
        """Cache bounded lexical weights once per load, with no external embedding service."""
        for server_name, manifest in sorted(self._servers.items()):
            for endpoint in sorted(manifest.endpoints, key=lambda value: value.function_name):
                key = f"{server_name}.{endpoint.function_name}"
                index: Counter[str] = Counter()
                fields = [
                    (endpoint.function_name, 8),
                    (server_name, 4),
                    (endpoint.summary, 3),
                    (endpoint.path, 2),
                    (endpoint.method, 1),
                    (" ".join(param.name for param in endpoint.parameters), 2),
                    (" ".join(field.name for field in endpoint.response_fields), 1),
                ]
                for text, weight in fields:
                    for token in set(self._tokens(text[:_MAX_INDEX_TEXT])):
                        index[token] += weight
                self._search_index[key] = index
                self._endpoints[key] = endpoint

    @staticmethod
    def _tokens(text: str) -> list[str]:
        """Tokenize camelCase, snake_case and URL path text deterministically."""
        return re.findall(r"[a-z0-9]+", re.sub(r"([a-z\d])([A-Z])", r"\1 \2", text).lower())

    def fingerprint(self) -> str:
        """Return the deterministic digest of all loaded manifest fields, including native schemas."""
        return self._fingerprint

    def search_functions(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Rank lexical matches with stable identifier ties and bounded query/result sizes.

        Args:
            query: Plain words, names or path fragments.
            limit: Maximum result count, capped at 100; nonpositive values return no results.

        Returns:
            Compact dictionaries containing identifiers, metadata and lexical score.
        """
        if limit <= 0:
            return []
        tokens = set(self._tokens(query[:_MAX_QUERY_CHARS]))
        ranked: list[tuple[int, str]] = []
        for key, index in self._search_index.items():
            score = sum(index.get(token, 0) for token in tokens)
            if tokens and not score:
                continue
            ranked.append((-score, key))
        result: list[dict[str, Any]] = []
        for score, key in sorted(ranked)[: min(limit, _MAX_SEARCH_RESULTS)]:
            endpoint = self._endpoints[key]
            server_name, function_name = key.split(".", 1)
            result.append(
                {
                    "server_name": server_name,
                    "function_name": function_name,
                    "tool_id": key,
                    "summary": endpoint.summary,
                    "method": endpoint.method,
                    "path": endpoint.path,
                    "score": -score,
                }
            )
        return result

    def list_servers(self) -> list[ServerInfo]:
        """Return stable compact metadata detached from internal manifest state."""
        result: list[ServerInfo] = []
        for name, manifest in sorted(self._servers.items()):
            endpoints = sorted(manifest.endpoints, key=lambda ep: ep.function_name)
            result.append(
                ServerInfo(
                    name=name,
                    description=manifest.description,
                    functions=[ep.function_name for ep in endpoints],
                    function_summaries={ep.function_name: ep.summary for ep in endpoints},
                )
            )
        return result

    def get_manifest(self, server_name: str) -> ServerManifest:
        """Return a defensive copy of the loaded native-schema manifest."""
        return self._get_server_manifest(server_name).model_copy(deep=True)

    def get_endpoint(self, server_name: str, function_name: str) -> EndpointManifest:
        """Return a defensive copy of one declared broker capability."""
        manifest = self._get_server_manifest(server_name)
        return self._find_endpoint(manifest, server_name, function_name).model_copy(deep=True)

    def get_function(self, server_name: str, function_name: str) -> FunctionInfo:
        """Inspect native metadata and call_tool usage without touching generated source."""
        endpoint = self.get_endpoint(server_name, function_name)
        arguments = self._example(endpoint.input_schema)
        metadata: dict[str, Any] = {
            "server_name": server_name,
            "function_name": function_name,
            "summary": endpoint.summary,
            "description": endpoint.summary,
            "parameters": endpoint.parameters,
            "response_fields": endpoint.response_fields,
            "return_type": endpoint.return_type,
            "method": endpoint.method,
            "path": endpoint.path,
            "source_code": f'result = await call_tool("{server_name}.{function_name}", {arguments!r})',
            "output_schema": endpoint.output_schema,
        }
        return FunctionInfo.model_validate(metadata)

    @staticmethod
    def _example(schema: dict[str, Any], depth: int = 0) -> Any:
        """Build a bounded typed call example from required input metadata."""
        if depth > 8:
            return None
        if "default" in schema:
            return schema["default"]
        if schema.get("enum"):
            return schema["enum"][0]
        kind = schema.get("type")
        if isinstance(kind, list):
            kind = next((item for item in kind if item != "null"), "null")
        if kind == "object":
            properties = schema.get("properties", {})
            return {key: Registry._example(properties.get(key, {}), depth + 1) for key in schema.get("required", [])}
        if kind == "array":
            return []
        return {"string": "", "integer": 0, "number": 0.0, "boolean": False}.get(str(kind))

    def get_function_source(self, server_name: str, function_name: str) -> str:
        """Return explicitly requested SDK source, never an executable broker implementation."""
        self.get_endpoint(server_name, function_name)  # Validate server and endpoint exist
        return self._get_function_source(server_name, function_name)

    def get_swagger_hash(self, server_name: str) -> str:
        """Return raw document identity for existing SDK tooling."""
        return self._get_server_manifest(server_name).swagger_hash

    def has_skills(self, server_name: str) -> bool:
        """Check contained documentation for registered identities only."""
        return self.skills_path(server_name) is not None

    def skills_path(self, server_name: str) -> Path | None:
        """Return a contained nonsymlink path after validating server membership."""
        self._get_server_manifest(server_name)
        path = contained_path(self._compiled_dir, server_name, "skills.md")
        return path if path.is_file() else None

    def _get_server_manifest(self, server_name: str) -> ServerManifest:
        """Look up an identity before constructing any filesystem paths."""
        if server_name not in self._servers:
            raise ServerNotFoundError("Server is not registered")
        return self._servers[server_name]

    def _find_endpoint(self, manifest: ServerManifest, server_name: str, function_name: str) -> EndpointManifest:
        """Find a declared function without interpreting its name as a path."""
        for endpoint in manifest.endpoints:
            if endpoint.function_name == function_name:
                return endpoint
        raise FunctionNotFoundError("Function is not registered for this server")

    def _get_function_source(self, server_name: str, function_name: str) -> str:
        """Read bounded explicit SDK source without falling back to the whole file."""
        key = f"{server_name}.{function_name}"
        if key in self._function_source_cache:
            return self._function_source_cache[key]
        path = contained_path(self._compiled_dir, server_name, "functions.py")
        if not path.is_file():
            return "SDK source is unavailable; run gryphon compile again"
        with path.open("rb") as stream:
            data = stream.read(_MAX_SOURCE_BYTES + 1)
        if len(data) > _MAX_SOURCE_BYTES:
            raise CompileError("SDK source exceeds size limit")
        snippet = self._extract_function_snippet(data.decode("utf-8"), function_name)
        if len(self._function_source_cache) >= _MAX_SOURCE_CACHE:
            self._function_source_cache.pop(next(iter(self._function_source_cache)))
        self._function_source_cache[key] = snippet
        return snippet

    def _extract_function_snippet(self, source: str, function_name: str) -> str:
        """Extract matching top-level SDK functions and response declarations, or fail closed."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return "SDK source is invalid; run gryphon compile again"
        lines = source.splitlines()
        # Derive the PascalCase prefix used for TypedDict class names
        prefix = "".join(word.capitalize() for word in function_name.split("_"))
        # Collect TypedDict classes whose names match this function's response types
        snippets: list[str] = []
        function: str | None = None
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.startswith(prefix):
                snippets.append("\n".join(lines[node.lineno - 1 : node.end_lineno]))
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id.startswith(prefix) for target in node.targets
            ):
                if (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "TypedDict"
                ):
                    snippets.append("\n".join(lines[node.lineno - 1 : node.end_lineno]))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
                function = "\n".join(lines[node.lineno - 1 : node.end_lineno])
        if function is None:
            return "SDK function is unavailable; run gryphon compile again"  # Never fall back to full source
        return "\n\n".join([*snippets, function])

    def _parse_parameters_summary(self, summary: str) -> list[ParamSchema]:
        """Parse legacy SDK display strings, never v2 authoritative metadata."""
        result: list[ParamSchema] = []
        for part in re.split(r"\),\s*", summary.rstrip(")")):
            part = part.strip()  # noqa: PLW2901
            if not part:
                continue
            try:
                name, rest = part.split("(", 1)
                kind, required = rest.split(",", 1)
                result.append(
                    ParamSchema(
                        name=name.strip(), location="query", param_type=kind.strip(), required="required" in required
                    )
                )
            except ValueError:
                result.append(ParamSchema(name=part, location="query", param_type="string"))
        return result

    def _parse_response_summary(self, summary: str) -> list[ResponseField]:
        """Parse old display text only for explicit legacy SDK utility callers."""
        if not summary or summary == "response data":
            return []
        return [ResponseField(name=field.strip(), field_type="string") for field in summary.split(",") if field.strip()]
