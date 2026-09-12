"""Bounded JSON-only UCP REST reference expansion through the existing network authority."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urldefrag, urljoin

from gryphon.compiler.ucp_profile import bounded_json, origin, secure_url
from gryphon.errors import CompileError
from gryphon.security.network import decode_json

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.security.network import NetworkClient

MAX_DOCUMENTS = 32
MAX_SECONDS = 30.0
_MAX_EXPANSION_NODES = 10000
_MAX_EXPANSION_DEPTH = 32
_UNSUPPORTED_REFERENCES = frozenset({"$dynamicRef", "$recursiveRef", "$anchor", "$dynamicAnchor"})


class ReferenceLoader:
    """Own a single import's document, byte, time and expansion budgets, never credentials."""

    def __init__(self, client: NetworkClient, config: GryphonConfig, max_bytes: int, schema_url: str) -> None:
        """Initialize bounded import-local state; the caller owns client cleanup."""
        self.client = client
        self.config = config
        self.remaining = max_bytes
        self.schema_origin = origin(schema_url)
        self.deadline = time.monotonic() + min(MAX_SECONDS, config.http_timeout_seconds)
        self.documents: dict[str, dict[str, Any]] = {}
        self.nodes = 0
        self.expansion_bytes = max_bytes

    def check_budget(self, depth: int = 0) -> None:
        """Bound synchronous expansion as well as awaits, including cached reference graphs."""
        self.nodes += 1
        if time.monotonic() >= self.deadline:
            raise CompileError("UCP import exceeded its total time limit")
        if self.nodes > _MAX_EXPANSION_NODES or depth > _MAX_EXPANSION_DEPTH:
            raise CompileError("UCP reference expansion exceeds structural limits")

    async def document(self, url: str) -> dict[str, Any]:
        """GET only approved origins, with a hard aggregate byte limit and no redirects."""
        self.check_budget()
        url = secure_url(url, self.config)
        if origin(url) != self.schema_origin:
            raise CompileError("UCP external references must stay on the advertised schema origin")
        if url in self.documents:
            return self.documents[url]
        if len(self.documents) >= MAX_DOCUMENTS or self.remaining <= 0:
            raise CompileError("UCP schema fetch count or aggregate byte limit exceeded")
        response = await self.client.request(
            "GET", url, max_bytes=self.remaining, timeout=self.deadline - time.monotonic()
        )
        self.remaining -= len(response.content)
        if self.remaining < 0:
            raise CompileError("UCP schema aggregate byte limit exceeded")
        document = decode_json(response)
        if not isinstance(document, dict):
            raise CompileError("UCP REST schema must be a JSON object")
        bounded_json(document, self.config.max_spec_size_bytes)
        self._check_scopes(document, url)
        self.documents[url] = document
        return document

    def _check_scopes(self, document: dict[str, Any], url: str) -> None:
        """Reject hidden reference rebasing even when a JSON pointer skips an ancestor."""
        nodes: list[Any] = [document]
        while nodes:
            node = nodes.pop()
            if isinstance(node, dict):
                if "$id" in node and node["$id"] != url:
                    raise CompileError("UCP schema identifiers that change reference scope are unsupported")
                if "$schema" in node and node["$schema"] != "https://json-schema.org/draft/2020-12/schema":
                    raise CompileError("UCP schema dialect is unsupported")
                nodes.extend(node.values())
            elif isinstance(node, list):
                nodes.extend(node)

    async def expand(self, node: Any, base: str, refs: tuple[str, ...] = (), depth: int = 0) -> Any:
        """Inline JSON pointers while rejecting cycles, rebasing IDs and ambiguous ref siblings."""
        self.check_budget(depth)
        if isinstance(node, list):
            return [await self.expand(item, base, refs, depth + 1) for item in node]
        if not isinstance(node, dict):
            self.expansion_bytes -= bounded_json(node, self.expansion_bytes)
            return node
        if _UNSUPPORTED_REFERENCES.intersection(node):
            raise CompileError("UCP dynamic or anchor references are unsupported")
        if "$id" in node and node["$id"] != base:
            raise CompileError("UCP schema identifiers that change reference scope are unsupported")
        if "$ref" in node:
            if set(node) - {"$ref", "description", "title", "$schema", "$id"}:
                raise CompileError("UCP validation siblings beside references are unsupported")
            target, target_base, identity = await self.reference(node["$ref"], base)
            if identity in refs:
                raise CompileError("UCP recursive references are unsupported")
            result = await self.expand(target, target_base, (*refs, identity), depth + 1)
            if not isinstance(result, dict):
                raise CompileError("UCP reference must resolve to an object")
            return {**result, **{key: node[key] for key in ("description", "title") if key in node}}
        return {
            key: await self.expand(value, base, refs, depth + 1)
            for key, value in node.items()
            if key not in {"$schema", "$id"}
        }

    async def resolve(self, node: Any, base: str) -> tuple[dict[str, Any], str]:
        """Resolve only a reference chain, without fetching unneeded nested response branches."""
        refs: set[str] = set()
        while True:
            self.check_budget(len(refs))
            if not isinstance(node, dict):
                raise CompileError("UCP reference must resolve to an object")
            if _UNSUPPORTED_REFERENCES.intersection(node):
                raise CompileError("UCP dynamic or anchor references are unsupported")
            if "$id" in node and node["$id"] != base:
                raise CompileError("UCP schema identifiers that change reference scope are unsupported")
            if "$ref" not in node:
                return {key: value for key, value in node.items() if key not in {"$schema", "$id"}}, base
            if set(node) - {"$ref", "description", "title", "$schema", "$id"}:
                raise CompileError("UCP validation siblings beside references are unsupported")
            target, base, identity = await self.reference(node["$ref"], base)
            if identity in refs:
                raise CompileError("UCP recursive references are unsupported")
            refs.add(identity)
            if not isinstance(target, dict):
                raise CompileError("UCP reference must resolve to an object")
            node = {**target, **{key: node[key] for key in ("description", "title") if key in node}}

    async def reference(self, ref: Any, base: str) -> tuple[Any, str, str]:
        """Resolve URI-relative remote JSON documents and RFC 6901 object/list pointers only."""
        if not isinstance(ref, str) or not ref or "\\" in ref or any(ord(char) <= 32 for char in ref):
            raise CompileError("UCP reference is invalid")
        url, fragment = urldefrag(urljoin(base, ref))
        url = secure_url(url, self.config)
        fragment = unquote(fragment)
        if fragment and not fragment.startswith("/"):
            raise CompileError("UCP schema anchors are unsupported; use JSON pointers")
        document = await self.document(url)
        if "$id" in document and document["$id"] != url:
            raise CompileError("UCP schema identifiers that change reference scope are unsupported")
        node: Any = document
        try:
            for token in fragment[1:].split("/") if fragment else []:
                token = token.replace("~1", "/").replace("~0", "~")
                if isinstance(node, list):
                    if not token.isdecimal() or str(int(token)) != token:
                        raise ValueError("Invalid array index")
                    node = node[int(token)]
                else:
                    node = node[token]
        except (KeyError, IndexError, TypeError, ValueError):
            raise CompileError("UCP reference target does not exist") from None
        return node, url, f"{url}#{fragment}"
