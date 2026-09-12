"""Bounded public URL imports without filesystem, credential or redirect authority."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from gryphon.compiler.documents import DEFAULT_MAX_DOCUMENT_BYTES
from gryphon.compiler.ucp import profile_to_openapi
from gryphon.errors import CapacityError, CompileError, ExecutionError, InputValidationError
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.saas_upload import inspect_upload
from gryphon.security.network import NetworkClient, decode_json
from gryphon.security.policies import validated_url

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.models import SpecImport


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

    async def load(self, name: str, *, content: Any = None, url: Any = None, kind: Any = "openapi") -> SpecImport:
        """Validate a file or fetch a public document, never accepting a caller-selected host file."""
        if not isinstance(kind, str) or kind not in {"openapi", "ucp"}:
            raise InputValidationError("Invalid specification kind")
        if self._lock.locked():
            raise CapacityError("Specification import is busy")
        async with self._lock, asyncio.timeout(25):
            if url is None:
                if not isinstance(content, str) or kind != "openapi":
                    raise InputValidationError("Invalid specification content")
                return await inspect_upload(content, self.config, self.limit, name)
            if content is not None or kind not in {"openapi", "ucp"}:
                raise InputValidationError("Invalid specification source")
            location = source_url(url, ucp=kind == "ucp")
            return await self._remote(name, location, kind)

    async def _remote(self, name: str, location: str, kind: str) -> SpecImport:
        """Fetch exactly the configured document through verified, bounded, DNS-pinned HTTP."""
        client = NetworkClient(self.config)
        try:
            response = await client.request(
                "GET",
                location,
                max_bytes=min(self.limit, DEFAULT_MAX_DOCUMENT_BYTES) if kind == "ucp" else self.limit,
                headers={
                    "Accept": "application/json" if kind == "ucp" else "application/json, application/yaml, text/plain"
                },
            )
            content = response.content.decode("utf-8")
        except (ExecutionError, UnicodeError):
            raise CompileError("Specification fetch failed") from None
        finally:
            await finish_cleanup(client.close())
        warnings = []
        if kind == "ucp":
            try:
                profile = await finish_cleanup(asyncio.to_thread(decode_json, response))
            except ExecutionError:
                raise CompileError("UCP profile must be JSON") from None
            if not isinstance(profile, dict):
                raise CompileError("UCP profile must be an object")
            document = await profile_to_openapi(
                profile, location, self.config, self.limit, profile_bytes=len(response.content)
            )
            warnings = document["x-gryphon-ucp"]["warnings"]
            content = json.dumps(document, ensure_ascii=False)
        result = await inspect_upload(content, self.config, self.limit, name, origin=location)
        result.source_url = location
        result.source_type = "ucp_url" if kind == "ucp" else "openapi_url"
        result.warnings = sorted(set(result.warnings + warnings))
        return result
