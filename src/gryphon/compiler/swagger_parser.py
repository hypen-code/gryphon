"""Parse the explicitly supported OpenAPI 3.0/3.1 and Swagger 2.0 v2 subset."""

from __future__ import annotations

import re
from typing import Any

import yaml

from gryphon.compiler.catalog import module_name, validate_base_url, validate_endpoint
from gryphon.compiler.documents import fetch_document, fetch_local, fetch_remote, github_blob_to_raw
from gryphon.compiler.schemas import SchemaParser, validate_document_tree
from gryphon.config import GryphonConfig
from gryphon.errors import CompileError
from gryphon.models import EndpointSpec, ParamSchema, RequestBodyMediaType, ServerSpec, SwaggerSource
from gryphon.utils.hashing import hash_content
from gryphon.utils.logging import get_logger

logger = get_logger(__name__)

# Methods that mutate state
_MUTATING_METHODS = {"post", "put", "patch", "delete"}
_github_blob_to_raw = github_blob_to_raw


class SwaggerParser(SchemaParser):
    """Normalize bounded schemas while preserving authoritative native input/output metadata."""

    def __init__(
        self,
        source: SwaggerSource,
        max_spec_size_bytes: int | None = None,
        *,
        config: GryphonConfig | None = None,
    ) -> None:
        """Initialize trusted source identity and explicit document-fetch policy."""
        module_name(source.name)
        self._source = source
        self._config = config if config is not None else GryphonConfig.model_construct()
        self._max_spec_size_bytes = (
            max_spec_size_bytes if max_spec_size_bytes is not None else self._config.max_spec_size_bytes
        )
        self._raw_doc: dict[str, Any] = {}
        self._components: dict[str, Any] = {}

    async def parse(self) -> ServerSpec:
        """Fetch and normalize one document without exposing raw parsing inputs in errors."""
        content = await self._fetch_document()
        try:
            self._raw_doc = self._load_document(content)
            self._components = self._raw_doc.get("components", {}).get("schemas", {})
            base = self._resolve_base_url()
            endpoints = self._parse_paths()
            spec = ServerSpec(
                name=self._source.name,
                description=self._extract_description(),
                base_url=base,
                is_read_only=self._source.is_read_only,
                endpoints=endpoints,
                swagger_hash=hash_content(content),
                server_url_vars=self._collect_extra_server_url_vars(base),
            )
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise CompileError("Invalid or unsupported specification structure") from None
        logger.info("swagger_parsed", server=module_name(self._source.name), total_endpoints=len(endpoints))
        return spec

    async def _fetch_document(self) -> str:
        """Read only the configured document using explicit application network policy."""
        return await fetch_document(self._source.swagger_url, self._max_spec_size_bytes, config=self._config)

    async def _fetch_remote(self, url: str) -> str:
        """Fetch with DNS pinning, verified TLS, no redirects and bounded bytes."""
        return await fetch_remote(url, self._max_spec_size_bytes, config=self._config)

    def _fetch_local(self, path: str) -> str:
        """Read a bounded local file without symlink traversal."""
        return fetch_local(path, self._max_spec_size_bytes)

    def _load_document(self, content: str) -> dict[str, Any]:
        """Parse YAML/JSON and reject unsupported dialects or unbounded document trees."""
        try:
            doc = yaml.safe_load(content)
        except (yaml.YAMLError, RecursionError):
            raise CompileError("Failed to parse swagger YAML/JSON") from None
        if not isinstance(doc, dict):
            raise CompileError("Swagger document is not a mapping")
        validate_document_tree(doc)
        version = str(doc.get("openapi", doc.get("swagger", "")))
        if not (version.startswith(("3.0.", "3.1.")) or version == "2.0"):
            raise CompileError("Unsupported OpenAPI version; supported subset: 3.0, 3.1 and Swagger 2.0")
        if not isinstance(doc.get("paths", {}), dict):
            raise CompileError("Invalid paths object")
        return doc

    def _collect_extra_server_url_vars(self, primary_url: str) -> dict[str, str]:
        """Collect SDK alternate destinations only when trusted config has no override."""
        if self._source.base_url:
            return {}
        result: dict[str, str] = {}
        candidates: list[Any] = []
        # Global servers (skip index 0 — that's the primary)
        candidates.extend(self._raw_doc.get("servers", [])[1:])
        # Path-item and operation-level servers
        for item in self._raw_doc.get("paths", {}).values():
            if isinstance(item, dict):
                candidates.extend(item.get("servers", []))
                for operation in item.values():
                    if isinstance(operation, dict):
                        candidates.extend(operation.get("servers", []))
        for server in candidates:
            if isinstance(server, dict) and server.get("url"):
                url = validate_base_url(server["url"])
                if url != primary_url and url not in result:
                    result[url] = f"_BASE_URL_{len(result) + 1}"
        return result

    def _resolve_base_url(self) -> str:
        """Resolve configured authority before considering validated document defaults."""
        # Priority 1: explicitly configured in swaggers.yaml
        if self._source.base_url:
            return validate_base_url(self._source.base_url)
        # Priority 2: fall back to the spec's servers[] block
        servers = self._raw_doc.get("servers", [])
        if servers:
            if not isinstance(servers[0], dict):
                raise CompileError("Invalid 'servers' entry; expected a mapping")
            if not servers[0].get("url"):
                raise CompileError("The servers[0].url is empty")
            return validate_base_url(servers[0]["url"])
        if self._raw_doc.get("swagger") == "2.0" and self._raw_doc.get("host"):
            schemes = self._raw_doc.get("schemes", ["https"])
            return validate_base_url(f"{schemes[0]}://{self._raw_doc['host']}{self._raw_doc.get('basePath', '')}")
        raise CompileError("No base URL found; configure base_url or a valid absolute servers URL")

    def _extract_description(self) -> str:
        """Extract descriptive document metadata, never authentication settings."""
        info = self._raw_doc.get("info", {})
        return str(info.get("description", info.get("title", self._source.name)))[:4000]

    def _parse_paths(self) -> list[EndpointSpec]:
        """Parse supported routes, rejecting normalized-name collisions or partial callable contracts."""
        endpoints: list[EndpointSpec] = []
        names: set[str] = set()
        for path, item in self._raw_doc.get("paths", {}).items():
            if not isinstance(item, dict) or "$ref" in item:
                raise CompileError("Invalid or unsupported path item reference")
            for method, operation in item.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                    continue
                try:
                    endpoint = self._parse_operation(
                        path, method.upper(), operation, item.get("parameters", []), item.get("servers", [])
                    )
                    if endpoint is not None:
                        if endpoint.operation_id in names:
                            raise CompileError("Operation names collide after Python normalization")
                        names.add(endpoint.operation_id)
                        endpoints.append(endpoint)
                except CompileError:  # Invalid input must never silently become a partial callable contract.
                    logger.warning("endpoint_rejected", method=method, reason="unsupported or ambiguous operation")
                    raise
        return endpoints

    @staticmethod
    def _resolve_endpoint_base_url(operation: dict[str, Any], path_level_servers: list[Any]) -> str:
        """Resolve validated SDK destinations from operation then path servers."""
        for servers in (operation.get("servers", []), path_level_servers):
            if servers:
                if not isinstance(servers, list) or not isinstance(servers[0], dict):
                    raise CompileError("Invalid operation servers")
                return validate_base_url(servers[0].get("url", ""))
        return ""

    def _parse_operation(
        self,
        path: str,
        method: str,
        operation: dict[str, Any],
        path_level_params: list[Any],
        path_level_servers: list[Any] | None = None,
    ) -> EndpointSpec | None:
        """Normalize one route, enforcing source read-only policy before parsing its schemas."""
        # Skip read-only violations
        read_only_post = self._read_only_post(path, method, operation, path_level_servers or [])
        if self._source.is_read_only and method.lower() in _MUTATING_METHODS and not read_only_post:
            logger.debug("skipped_readonly_method", method=method)
            return None
        if not isinstance(operation, dict):
            raise CompileError("Invalid operation object")
        operation_id = self._sanitize_identifier(
            operation.get("operationId") or self._generate_operation_id(method, path)
        )
        raw_params = list(path_level_params) + list(operation.get("parameters", []))
        body_schema, parameters, media_type = self._operation_inputs(operation, raw_params)
        # Auto-detect path params from URL template not explicitly declared in spec
        declared = {param.name for param in parameters if param.location == "path"}
        for name in re.findall(r"\{([^}]+)\}", path):
            if name not in declared:
                parameters.append(ParamSchema(name=name, location="path", param_type="string", required=True))
        # Server URL priority: trusted source config overrides all document destinations.
        # Empty string means "fall back to the compiled server base URL".
        base = "" if self._source.base_url else self._resolve_endpoint_base_url(operation, path_level_servers or [])
        response = self._success_response_schema(operation.get("responses", {}))
        endpoint = EndpointSpec(
            path=path,
            method=method,
            operation_id=operation_id,
            summary=str(operation.get("summary", operation.get("description", f"{method} {path}")))[:200],
            description=str(operation.get("description", ""))[:1000],
            parameters=parameters,
            request_body_schema=body_schema,
            request_body_media_type=media_type,
            read_only_post=read_only_post,
            response_schema=self._schema_to_fields(response, 0),
            response_json_schema=response,
            tags=operation.get("tags", []),
            base_url=base,
        )
        validate_endpoint(endpoint)
        return endpoint

    def _read_only_post(self, path: str, method: str, operation: dict[str, Any], servers: list[Any]) -> bool:
        """Consult only trusted operator permits against the effective destination."""
        if method != "POST" or not self._config.allowed_read_only_post_operations:
            return False
        if not isinstance(operation, dict):
            raise CompileError("Invalid operation object")
        base = self._source.base_url or self._resolve_endpoint_base_url(operation, servers) or self._resolve_base_url()
        base = validate_base_url(base)
        return any(
            permit.matches(module_name(self._source.name), method, base, path)
            for permit in self._config.allowed_read_only_post_operations
        )

    def _operation_inputs(
        self,
        operation: dict[str, Any],
        raw_params: list[Any],
    ) -> tuple[dict[str, Any] | None, list[ParamSchema], RequestBodyMediaType]:
        """Normalize structured bodies with explicit requiredness and wire media type."""
        body = operation.get("requestBody", {})
        if "$ref" in body:
            body = self._resolve_ref(body["$ref"]) or {}
            if not body:
                raise CompileError("Unsupported request body reference")
        media_type = self._request_body_media_type(body)
        schema = self._parse_request_body(body)
        ordinary: list[Any] = []
        for raw in raw_params:
            resolved = self._resolve_ref(raw["$ref"]) if isinstance(raw, dict) and "$ref" in raw else raw
            if isinstance(resolved, dict) and resolved.get("in") == "body":
                if schema is not None:
                    raise CompileError("Multiple request bodies are unsupported")
                consumes = operation.get("consumes", self._raw_doc.get("consumes", ["application/json"]))
                if "application/json" not in consumes:
                    raise CompileError("Unsupported Swagger request body media type")
                schema = self._normalize_schema(resolved.get("schema", {}))
                body = resolved
            else:
                ordinary.append(raw)
        params = self._parse_parameters(ordinary)
        if schema is not None:
            params.append(
                ParamSchema(
                    name="json_body",
                    location="body",
                    param_type=self._extract_type(schema),
                    required=bool(body.get("required", False)),
                    description=f"Structured request body encoded as {media_type}",
                    json_schema=schema,
                )
            )
        return schema, params, media_type

    def _generate_operation_id(self, method: str, path: str) -> str:
        """Generate a stable identifier when operationId is absent."""
        sanitized = re.sub(r"[^a-zA-Z0-9_/]", "_", path)
        parts = [part for part in sanitized.split("/") if part and part != "_"]
        return f"{method.lower()}_{'_'.join(parts)}"

    def _sanitize_identifier(self, name: str) -> str:
        """Normalize valid operation labels after rejecting malformed source fragments."""
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_ .:/{}-]+", name):
            raise CompileError("Malformed operation name")
        # Split camelCase/PascalCase boundaries before lowercasing
        name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
        name = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", name)
        # Replace non-identifier characters with underscores
        name = re.sub(r"_+", "_", re.sub(r"[^a-zA-Z0-9_]", "_", name)).strip("_").lower()
        return f"fn_{name}" if name and name[0].isdigit() else name or "endpoint"
