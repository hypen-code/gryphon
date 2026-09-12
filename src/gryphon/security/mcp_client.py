"""Fresh, unauthenticated native MCP sessions over DNS-pinned bounded HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_HANDSHAKE_VERSION

from gryphon.errors import ConflictError, ExecutionError, SecurityViolationError, UpstreamDiagnosticError
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.security.mcp_protocol import (
    MAX_NOTIFICATIONS,
    normalize_content,
    parse_json,
    rpc_record,
    session_header,
    validate_acknowledgement,
)
from gryphon.security.network import NetworkClient
from gryphon.security.response import validate_response
from gryphon.security.ucp_identity import configured_profile, validate_profile_addresses
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from gryphon.config import GryphonConfig
    from gryphon.models.diagnostics import DiagnosticPhase

MAX_TOOLS = 1000
MAX_PAGES = 100
MAX_CURSOR_BYTES = 4096
MAX_DISCOVERY_BYTES = 5 * 1024 * 1024
MAX_DISCOVERY_SECONDS = 30.0
MAX_TOOL_NAME = 128
MAX_CLEANUP_SECONDS = 2.0
MAX_CLEANUP_BYTES = 1024
logger = get_logger(__name__)


def tool_fingerprint(tool: dict[str, Any]) -> str:
    """Hash canonical raw name/input/output metadata, without schema normalization.

    Args:
        tool: Native MCP tool definition; descriptions and hints are not authority.

    Returns:
        Lowercase SHA-256 digest binding the exact exposed schemas and remote name.
    """
    _validate_tool(tool)
    metadata = {key: tool.get(key) for key in ("name", "inputSchema", "outputSchema")}
    try:
        content = json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ExecutionError("Remote MCP returned invalid tool metadata") from None


def _validate_tool(tool: Any) -> None:
    """Validate native schema containers while retaining their exact raw contents."""
    if not isinstance(tool, dict):
        raise ExecutionError("Remote MCP returned invalid tool metadata")
    name, schema = tool.get("name"), tool.get("inputSchema")
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_TOOL_NAME:
        raise ExecutionError("Remote MCP returned invalid tool metadata")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ExecutionError("Remote MCP returned invalid tool metadata")
    if "outputSchema" in tool:
        output = tool["outputSchema"]
        if not isinstance(output, dict) or output.get("type") != "object":
            raise ExecutionError("Remote MCP returned invalid tool metadata")
    if "description" in tool and not isinstance(tool["description"], str):
        raise ExecutionError("Remote MCP returned invalid tool metadata")


class _Session:
    """Own one endpoint, ephemeral protocol/session state, and an aggregate byte budget."""

    def __init__(
        self,
        client: NetworkClient,
        endpoint: str,
        max_bytes: int,
        deadline: float,
        ucp_agent_profile: str | None = None,
    ) -> None:
        """Keep session authority local to a single discovery or invocation."""
        self.client, self.endpoint = client, endpoint
        self.remaining, self.deadline = max_bytes, deadline
        self.protocol = LATEST_HANDSHAKE_VERSION
        self.session_id: str | None = None
        self.initialized = False
        self.notifications = 0
        self.ucp_agent_profile = ucp_agent_profile

    def _headers(self) -> dict[str, str]:
        """Build only native endpoint-local protocol headers, never inherited authority."""
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": self.protocol}
        if self.ucp_agent_profile is not None:
            headers["UCP-Agent"] = f'profile="{self.ucp_agent_profile}"'
        if self.session_id is not None:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def validate_data(self, value: Any) -> Any:
        """Keep session identifiers out of snapshot metadata and returned application data."""
        sensitive = {"Mcp-Session-Id": self.session_id} if self.session_id is not None else {}
        return validate_response(value, {}, sensitive)

    async def _post(self, body: dict[str, Any], response_id: str | None) -> httpx.Response:
        """Send once without credentials, redirects, cookies, or resumable retries."""
        response = await self.client.request(
            "POST",
            self.endpoint,
            headers=self._headers(),
            json_body=body,
            timeout=self.deadline - asyncio.get_running_loop().time(),
            max_bytes=self.remaining,
            rpc_response_id=response_id,
        )
        self.remaining -= response.extensions.get("mcp_consumed_bytes", len(response.content))
        self.notifications += response.extensions.get("mcp_notifications", 0)
        if self.remaining < 0 or self.notifications > MAX_NOTIFICATIONS:
            raise ExecutionError("Remote MCP session budget exceeded")
        returned_session = session_header(response.headers)
        if body["method"] == "initialize":
            self.session_id = returned_session
        elif returned_session is not None and returned_session != self.session_id:
            raise ExecutionError("Remote MCP changed its session identifier")
        return response

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Match one fresh request identity and reject non-JSON or unowned results."""
        response_id = uuid4().hex
        phase: DiagnosticPhase = "invoke" if method == "tools/call" else "discovery"
        try:
            response = await self._post(
                {"jsonrpc": "2.0", "id": response_id, "method": method, "params": params}, response_id
            )
        except UpstreamDiagnosticError as exc:
            raise UpstreamDiagnosticError(exc.diagnostic.model_copy(update={"phase": phase})) from None
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise ExecutionError("Remote MCP returned an unsupported response type")
        record = rpc_record(parse_json(response.content), response_id, phase=phase)
        if record is None:
            raise ExecutionError("Remote MCP returned no matching response")
        result: dict[str, Any] = record["result"]
        return result

    async def initialize(self) -> None:
        """Negotiate an SDK-supported handshake version and acknowledge initialization."""
        result = await self.request(
            "initialize",
            {
                "protocolVersion": self.protocol,
                "capabilities": {},
                "clientInfo": {"name": "gryphon", "version": "2.0.0"},
            },
        )
        protocol = result.get("protocolVersion")
        capabilities, server = result.get("capabilities"), result.get("serverInfo")
        if not isinstance(protocol, str) or protocol not in HANDSHAKE_PROTOCOL_VERSIONS:
            raise ExecutionError("Remote MCP selected an unsupported protocol version")
        if (
            not isinstance(capabilities, dict)
            or not isinstance(capabilities.get("tools"), dict)
            or not isinstance(server, dict)
            or not isinstance(server.get("name"), str)
            or not isinstance(server.get("version"), str)
        ):
            raise ExecutionError("Remote MCP returned invalid initialization metadata")
        self.protocol = protocol
        self.initialized = True
        response = await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)
        validate_acknowledgement(response)

    async def tools(self) -> list[dict[str, Any]]:
        """Collect bounded native pages, rejecting cycles and duplicate tool names."""
        tools: list[dict[str, Any]] = []
        names: set[str] = set()
        cursors: set[str] = set()
        params: dict[str, Any] = {}
        for _ in range(MAX_PAGES):
            page = await self.request("tools/list", params)
            entries = page.get("tools")
            if not isinstance(entries, list) or len(tools) + len(entries) > MAX_TOOLS:
                raise ExecutionError("Remote MCP tool listing limit exceeded")
            self.validate_data(entries)
            for entry in entries:
                _validate_tool(entry)
                if entry["name"] in names:
                    raise ExecutionError("Remote MCP returned duplicate tool names")
                names.add(entry["name"])
                tools.append(entry)
            cursor = page.get("nextCursor")
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or not 1 <= len(cursor.encode("utf-8")) <= MAX_CURSOR_BYTES:
                raise ExecutionError("Remote MCP returned an invalid pagination cursor")
            if cursor in cursors:
                raise ExecutionError("Remote MCP returned a pagination cycle")
            cursors.add(cursor)
            params = {"cursor": cursor}
        raise ExecutionError("Remote MCP pagination limit exceeded")

    async def close(self) -> None:
        """Best-effort terminate only a verified owned session, then always disconnect."""
        try:
            if self.initialized and self.session_id is not None:
                headers = self._headers()
                self.session_id = None
                self.initialized = False
                await self._delete(headers)
        finally:
            self.session_id = None
            await self.client.close()

    async def _delete(self, headers: dict[str, str]) -> None:
        """Allow one separate two-second/1-KiB cleanup budget without changing results."""
        try:
            async with asyncio.timeout(MAX_CLEANUP_SECONDS):
                await self.client.request(
                    "DELETE",
                    self.endpoint,
                    headers=headers,
                    timeout=MAX_CLEANUP_SECONDS,
                    max_bytes=MAX_CLEANUP_BYTES,
                )
        except (ExecutionError, SecurityViolationError, httpx.HTTPError, OSError, TimeoutError, ValueError):
            logger.warning("mcp_session_cleanup_failed")


@asynccontextmanager
async def _session(
    config: GryphonConfig,
    endpoint: str,
    max_bytes: int,
    timeout: float,
    ucp_agent_profile: str | None = None,
    *,
    phase: DiagnosticPhase = "invoke",
    check_active: Callable[[], None] | None = None,
) -> AsyncIterator[_Session]:
    """Bound protocol work; separately allow owned cleanup after completion or cancellation."""
    if type(max_bytes) is not int or max_bytes <= 0 or not math.isfinite(timeout) or timeout <= 0:
        raise ExecutionError("Remote MCP session budget is invalid")
    deadline = asyncio.get_running_loop().time() + timeout
    session = _Session(NetworkClient(config), endpoint, max_bytes, deadline, ucp_agent_profile)
    try:
        async with asyncio.timeout_at(deadline):
            if ucp_agent_profile is not None:
                await validate_profile_addresses(ucp_agent_profile, config, phase=phase)
            if check_active is not None:
                check_active()
            await session.initialize()
            yield session
    except (TimeoutError, httpx.HTTPError, OSError, ValueError, TypeError, RecursionError):
        raise ExecutionError("Remote MCP session failed") from None
    finally:
        await finish_cleanup(session.close())


async def discover_tools(config: GryphonConfig, endpoint: str, max_bytes: int) -> list[dict[str, Any]]:
    """Discover native metadata only, with no tool invocations or resource fetching.

    Args:
        config: Host-owned egress and HTTP limits; no authentication is inherited.
        endpoint: Approved fixed MCP endpoint, not a server-selected follow-up URL.
        max_bytes: Aggregate raw response budget including initialization and all pages.

    Returns:
        Exact native definitions, limited to 1000 tools, 100 pages, and 5 MiB.
        Owned session DELETE adds a separate maximum two-second/1-KiB cleanup budget.
    """
    limit = min(max_bytes, config.max_spec_size_bytes, MAX_DISCOVERY_BYTES)
    timeout = min(config.http_timeout_seconds, MAX_DISCOVERY_SECONDS)
    profile = configured_profile(config, phase="discovery")
    async with _session(config, endpoint, limit, timeout, profile, phase="discovery") as session:
        return await session.tools()


def _tool_result(result: dict[str, Any]) -> Any:
    """Prefer structured data or one finite JSON text block; never fetch resources."""
    if "isError" in result and type(result["isError"]) is not bool:
        raise ExecutionError("Remote MCP returned an invalid tool result")
    if result.get("isError"):
        raise ExecutionError("Remote MCP tool execution failed")
    if "structuredContent" in result:
        if not isinstance(result["structuredContent"], dict):
            raise ExecutionError("Remote MCP returned invalid structured content")
        return result["structuredContent"]
    content = result.get("content")
    if not isinstance(content, list) or any(
        not isinstance(block, dict) or not isinstance(block.get("type"), str) for block in content
    ):
        raise ExecutionError("Remote MCP returned invalid tool content")
    return normalize_content(content)


async def invoke_tool(
    config: GryphonConfig,
    endpoint: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    expected_fingerprint: str,
    timeout: float,
    max_bytes: int,
    check_active: Callable[[], None] | None = None,
    ucp_agent_profile: str | None = None,
) -> Any:
    """Initialize a fresh session, check metadata drift, and call exactly once.

    Args:
        config: Host-owned egress policy, without ambient authentication.
        endpoint: Manifest-owned MCP URL; it cannot be replaced by remote metadata.
        tool_name: Exact native name already authorized by the broker.
        arguments: Broker-validated native input object.
        expected_fingerprint: Imported raw name/input/output SHA-256 binding.
        timeout: Absolute total-operation duration, including DNS and metadata discovery.
        max_bytes: Aggregate response byte limit across initialization, listing, and call.
        check_active: Optional broker-owned revocation check between protocol stages.
        ucp_agent_profile: Effective public UCP identity; fixed header only, never host credentials.

    Returns:
        Structured content, one finite JSON text value, or unchanged native content blocks.
        Owned session DELETE adds a separate maximum two-second/1-KiB cleanup budget.

    Raises:
        ConflictError: The selected tool disappeared or its raw schema binding changed.
        ExecutionError: Protocol, budget, transport, or remote execution failure.
    """
    if not isinstance(arguments, dict) or not isinstance(tool_name, str):
        raise ExecutionError("Remote MCP invocation is invalid")
    check = check_active if check_active is not None else lambda: None
    check()
    async with _session(config, endpoint, max_bytes, timeout, ucp_agent_profile, check_active=check) as session:
        check()
        tools = await session.tools()
        check()
        tool = next((entry for entry in tools if entry["name"] == tool_name), None)
        if tool is None or tool_fingerprint(tool) != expected_fingerprint:
            raise ConflictError("Remote MCP tool metadata changed; refresh the imported specification")
        result = await session.request("tools/call", {"name": tool_name, "arguments": arguments})
        check()
        return session.validate_data(_tool_result(result))
