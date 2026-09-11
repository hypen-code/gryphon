"""The sole host-owned upstream capability broker for restricted executions."""

from __future__ import annotations

import asyncio
import math
import re
import time
from typing import TYPE_CHECKING, Any

from gryphon.errors import CapacityError, ExecutionTimeoutError, SecurityViolationError
from gryphon.security.auth import AsyncVault
from gryphon.security.encoding import encode_request, validate_arguments
from gryphon.security.network import NetworkClient, decode_json
from gryphon.security.policies import check_domain_allowed, enforce_read_only, validated_url
from gryphon.security.response import validate_response

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.models import AuthConfig, EndpointManifest, ExecutionScope, ServerManifest
    from gryphon.runtime.registry import Registry

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")


class ToolBroker:
    """Interpret manifest metadata instead of importing generated endpoint code."""

    def __init__(
        self,
        config: GryphonConfig,
        registry: Registry,
        auth_configs: dict[str, AuthConfig] | None = None,
    ) -> None:
        """Create the broker-local verified connection pool and credential cache.

        Args:
            config: Trusted administrator network/write/resource policy.
            registry: Loaded v2 manifest registry.
            auth_configs: Host-only authentication configuration by server.
        """
        self._config = config
        self._registry = registry
        self._auth_configs = dict(auth_configs or {})
        self._network = NetworkClient(config)
        self._vault = AsyncVault(self._network)
        self._closed = False

    async def invoke(
        self,
        server_name: str,
        function_name: str,
        arguments: dict[str, Any],
        scope: ExecutionScope,
    ) -> Any:
        """Invoke one declared capability within a revocable execution budget.

        Args:
            server_name: Exact registered server identifier.
            function_name: Exact registered function identifier.
            arguments: Closed JSON object using OpenAPI wire parameter names.
            scope: Server-owned authority, counter and monotonic deadline.

        Returns:
            Schema-validated JSON without credential reflections, or None for no content.
        """
        self._check_scope(scope)
        if scope.calls >= min(scope.max_calls, self._config.max_tool_calls):
            raise CapacityError("Execution tool-call budget exhausted")
        scope.calls += 1
        if not _IDENTIFIER.fullmatch(server_name) or not _IDENTIFIER.fullmatch(function_name):
            raise SecurityViolationError("Invalid capability identifier")
        manifest = self._registry.get_manifest(server_name)
        endpoint = self._registry.get_endpoint(server_name, function_name)
        self._authorize(manifest, endpoint, server_name, function_name)
        normalized = validate_arguments(endpoint, arguments)
        url, headers, body = encode_request(endpoint, endpoint.base_url or manifest.base_url, normalized)
        check_domain_allowed(url, self._config.allowed_domains)
        try:
            async with asyncio.timeout(scope.deadline - time.monotonic()):
                return await self._send(
                    server_name,
                    manifest,
                    endpoint,
                    url,
                    headers,
                    body,
                    scope,
                    body_present="json_body" in normalized,
                )
        except TimeoutError:
            raise ExecutionTimeoutError("Execution deadline exceeded during capability call") from None

    async def _send(
        self,
        server_name: str,
        manifest: ServerManifest,
        endpoint: EndpointManifest,
        url: str,
        headers: dict[str, str],
        body: Any,
        scope: ExecutionScope,
        *,
        body_present: bool,
    ) -> Any:
        """Resolve host credentials and recheck authority before and after network I/O."""
        credentials = await self._vault.resolve(server_name, self._auth_configs.get(server_name))
        self._check_credential_origin(manifest.base_url, url, credentials)
        if {key.lower() for key in headers} & {key.lower() for key in credentials}:
            raise SecurityViolationError("Request header override is not permitted")
        self._check_scope(scope)
        response = await self._network.request(
            endpoint.method,
            url,
            headers={**headers, **credentials},
            json_body=body,
            json_body_present=body_present,
            timeout=scope.deadline - time.monotonic(),
        )
        self._check_scope(scope)
        if response.status_code == 204 or endpoint.method == "HEAD":
            return None
        result = validate_response(decode_json(response), endpoint.output_schema, credentials)
        self._check_scope(scope)
        return result

    def _authorize(
        self,
        manifest: ServerManifest,
        endpoint: EndpointManifest,
        server_name: str,
        function_name: str,
    ) -> None:
        """Enforce source read-only policy plus exact administrator write grants."""
        if endpoint.method not in _READ_METHODS | _WRITE_METHODS:
            raise SecurityViolationError("Unsupported upstream HTTP method")
        if manifest.is_read_only:
            enforce_read_only(endpoint.method, server_name)
        if endpoint.method in _WRITE_METHODS:
            operation = f"{server_name}.{function_name}"
            if not self._config.allow_writes or operation not in self._config.allowed_write_operations:
                raise SecurityViolationError("Write operation is not authorized by administrator policy")

    def _check_scope(self, scope: ExecutionScope) -> None:
        """Check live host-owned scope state before and after each asynchronous stage."""
        if self._closed or scope.cancelled:
            raise SecurityViolationError("Execution authority has been revoked")
        if not math.isfinite(scope.deadline) or time.monotonic() >= scope.deadline:
            raise ExecutionTimeoutError("Execution deadline exceeded")
        if scope.calls < 0 or scope.max_calls < 1:
            raise CapacityError("Execution tool-call budget is invalid")

    def _check_credential_origin(self, base_url: str, url: str, credentials: dict[str, str]) -> None:
        """Never delegate primary-server credentials to a different endpoint origin."""
        base, target = validated_url(base_url), validated_url(url)
        if credentials and (base.scheme, base.host, base.port) != (target.scheme, target.host, target.port):
            raise SecurityViolationError("Credentials cannot be delegated to a different upstream origin")

    async def close(self) -> None:
        """Revoke future calls, erase cached credentials and close all connections."""
        self._closed = True
        self._vault.close()
        await self._network.close()
