"""Bounded public URL imports without filesystem, credential or redirect authority."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from gryphon.compiler.catalog import module_name
from gryphon.compiler.ucp_discovery import discover_ucp
from gryphon.errors import CapacityError, CompileError, ExecutionError, InputValidationError
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.saas_post_reads import list_post_read_candidates, select_post_reads
from gryphon.saas_upload import inspect_mcp, inspect_upload
from gryphon.security.network import NetworkClient
from gryphon.security.policies import validated_url

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.models import ReadOnlyPostOperation, SaaSSpec, SpecImport


def source_url(value: Any, *, ucp: bool = False) -> str:
    """Accept public-document URLs without credential-bearing queries or ambiguous authority."""
    if not isinstance(value, str) or len(value) > 2048 or "${" in value:
        raise InputValidationError("Invalid specification URL")
    parsed = validated_url(value)
    if parsed.query or (ucp and parsed.scheme != "https"):
        raise InputValidationError("Specification URLs cannot contain queries; UCP requires HTTPS")
    if ucp and parsed.path in {"", "/"}:
        parsed = parsed.copy_with(path="/.well-known/ucp")
    return str(parsed)


class SpecImporter:
    """Admit one bounded import at a time for the control plane and close owned network resources."""

    def __init__(self, config: GryphonConfig, limit: int) -> None:
        """Borrow trusted operator policy without reading environment configuration."""
        self.config, self.limit = config, min(limit, config.max_spec_size_bytes)
        self._lock = asyncio.Lock()

    async def load(
        self,
        name: str,
        *,
        content: Any = None,
        url: Any = None,
        kind: Any = "openapi",
        read_only_filter: Any = True,
        previous: SaaSSpec | None = None,
    ) -> SpecImport:
        """Validate a file or fetch a public document, never accepting a caller-selected host file."""
        if type(read_only_filter) is not bool:
            raise InputValidationError("Read-only filter must be a boolean")
        if not isinstance(kind, str) or kind not in {"openapi", "ucp"}:
            raise InputValidationError("Invalid specification kind")
        if self._lock.locked():
            raise CapacityError("Specification import is busy")
        async with self._lock, asyncio.timeout(25):
            if url is None:
                if not isinstance(content, str) or kind != "openapi":
                    raise InputValidationError("Invalid specification content")
                return await inspect_upload(
                    content, self.config, self.limit, name, read_only_filter=read_only_filter, previous=previous
                )
            if content is not None or kind not in {"openapi", "ucp"}:
                raise InputValidationError("Invalid specification source")
            location = source_url(url, ucp=kind == "ucp")
            if kind == "ucp":
                location = str(validated_url(url))
            return await self._remote(name, location, kind, read_only_filter, previous)

    async def post_read_candidates(self, previous: SaaSSpec) -> list[dict[str, Any]]:
        """List canonical POST review rows under the shared bounded import admission gate."""
        if self._lock.locked():
            raise CapacityError("Specification import is busy")
        async with self._lock, asyncio.timeout(25):
            return await list_post_read_candidates(previous, self.config, self.limit)

    async def select_post_reads(self, previous: SaaSSpec, functions: Any) -> list[ReadOnlyPostOperation]:
        """Validate an administrator's replacement selection, never accepting submitted route tuples."""
        if self._lock.locked():
            raise CapacityError("Specification import is busy")
        async with self._lock, asyncio.timeout(25):
            return await select_post_reads(previous, functions, self.config, self.limit)

    async def refilter(self, previous: SaaSSpec, read_only_filter: Any) -> SpecImport:
        """Revalidate saved bytes without refetching remote sources or altering provenance."""
        if previous.mcp_bindings:
            if self._lock.locked():
                raise CapacityError("Specification import is busy")
            async with self._lock, asyncio.timeout(25):
                return await inspect_mcp(
                    previous, self.config, self.limit, module_name(previous.name), read_only_filter
                )
        content = await finish_cleanup(
            asyncio.to_thread(json.dumps, previous.document, ensure_ascii=False, separators=(",", ":"))
        )
        imported = await self.load(
            module_name(previous.name), content=content, read_only_filter=read_only_filter, previous=previous
        )
        imported.source_type, imported.source_url = previous.source_type, previous.source_url
        imported.resolved_profile_url = previous.resolved_profile_url
        imported.resolved_endpoint = previous.resolved_endpoint
        imported.source_transport = previous.source_transport
        if previous.source_type == "ucp_url":
            imported.warnings = sorted(set(imported.warnings + previous.document["x-gryphon-ucp"]["warnings"]))
        return imported

    async def _remote(
        self,
        name: str,
        location: str,
        kind: str,
        read_only_filter: bool,
        previous: SaaSSpec | None = None,
    ) -> SpecImport:
        """Fetch exactly the configured document through verified, bounded, DNS-pinned HTTP."""
        client = NetworkClient(self.config)
        try:
            if kind == "ucp":
                return await self._ucp(name, location, read_only_filter, previous, client)
            response = await client.request(
                "GET",
                location,
                max_bytes=self.limit,
                headers={"Accept": "application/json, application/yaml, text/plain"},
            )
            content = response.content.decode("utf-8")
        except (ExecutionError, UnicodeError):
            raise CompileError("Specification fetch failed") from None
        finally:
            await finish_cleanup(client.close())
        result = await inspect_upload(
            content,
            self.config,
            self.limit,
            name,
            origin=location,
            read_only_filter=read_only_filter,
            previous=previous,
        )
        result.source_url, result.source_type = location, "openapi_url"
        return result

    async def _ucp(
        self,
        name: str,
        location: str,
        read_only_filter: bool,
        previous: SaaSSpec | None,
        client: NetworkClient,
    ) -> SpecImport:
        """Resolve profiles/endpoints before validating a trusted transport-aware snapshot."""
        snapshot = await discover_ucp(self.config, location, self.limit, client)
        if snapshot.mcp_bindings:
            result = await inspect_mcp(snapshot, self.config, self.limit, name, read_only_filter)
        else:
            result = await inspect_upload(
                json.dumps(snapshot.document),
                self.config,
                self.limit,
                name,
                origin=location,
                read_only_filter=read_only_filter,
                previous=previous,
            )
            result.resolved_profile_url = snapshot.resolved_profile_url
            result.resolved_endpoint = snapshot.resolved_endpoint
            result.source_transport = snapshot.source_transport
            result.warnings = sorted(set(result.warnings + snapshot.warnings))
        result.source_url, result.source_type = location, "ucp_url"
        return result
