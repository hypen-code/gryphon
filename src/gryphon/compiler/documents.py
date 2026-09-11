"""Bounded local reads and DNS-pinned remote document fetching for compilation."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from gryphon.config import GryphonConfig
from gryphon.errors import ExecutionError, SecurityViolationError, SwaggerFetchError
from gryphon.security.network import NetworkClient

DEFAULT_MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
_GITHUB_BLOB_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/blob/(.+)$")


def github_blob_to_raw(url: str) -> str:
    """Convert a configured GitHub blob URL to its raw document equivalent."""
    match = _GITHUB_BLOB_RE.match(url)
    if match:
        return f"https://raw.githubusercontent.com/{match.group(1)}/{match.group(2)}"
    return url


async def fetch_remote(
    url: str,
    max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
    *,
    config: GryphonConfig | None = None,
) -> str:
    """Fetch through host-owned DNS pinning, address policy, TLS verification and byte bounds.

    Args:
        url: Explicitly configured specification or skills URL, never external schema metadata.
        max_bytes: Hard response-byte limit.
        config: Trusted network policy; absent config uses declared secure defaults, not credentials.

    Returns:
        UTF-8 document content without following redirects or external references.
    """
    policy = config if config is not None else GryphonConfig.model_construct()
    try:
        client = NetworkClient(policy)
        try:
            response = await client.request(
                "GET",
                github_blob_to_raw(url),
                headers={"Accept": "application/json, application/yaml, text/plain"},
                max_bytes=max_bytes,
            )
            return response.content.decode("utf-8")
        finally:
            await client.close()
    except (ExecutionError, SecurityViolationError, httpx.HTTPError, OSError, UnicodeError, ValueError):
        raise SwaggerFetchError("Failed to fetch document securely within configured policy and size limit") from None


def fetch_local(path: str, max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES) -> str:
    """Read an explicitly configured bounded UTF-8 file without symlink traversal."""
    file = Path(path).absolute()
    try:
        if any(node.is_symlink() for node in (file, *file.parents)):
            raise SwaggerFetchError("Symlink document paths are not permitted")
        with file.open("rb") as stream:
            content = stream.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise SwaggerFetchError("Document exceeds configured maximum size")
        return content.decode("utf-8")
    except (OSError, UnicodeError):
        raise SwaggerFetchError("Failed to read document file") from None


async def fetch_document(
    url: str,
    max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
    *,
    config: GryphonConfig | None = None,
) -> str:
    """Load only an explicitly configured local file or policy-validated HTTP(S) document."""
    if url.startswith(("http://", "https://")):
        return await fetch_remote(url, max_bytes, config=config)
    if "://" in url:
        raise SwaggerFetchError("Unsupported document URL scheme")
    return fetch_local(url, max_bytes)
