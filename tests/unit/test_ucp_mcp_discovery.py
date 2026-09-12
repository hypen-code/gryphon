"""UCP origin-profile inference uses only pinned metadata requests, never HTML or redirects."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest
from test_ucp_mcp import ENDPOINT, LIMIT, VERSION, tool

from gryphon.compiler import ucp_discovery
from gryphon.compiler.ucp_mcp import READ_CAPABILITIES
from gryphon.errors import ExecutionError, SecurityViolationError, UCPImportError
from gryphon.saas_spec_import import SpecImporter
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig

ORIGIN = "https://merchant.example"
PROFILE_URL = ORIGIN + "/.well-known/ucp"


def profile() -> dict[str, Any]:
    """Advertise the current MCP-only public Shopify custom-domain delegation shape."""
    return {
        "ucp": {
            "version": VERSION,
            "services": {
                "dev.ucp.shopping": [
                    {
                        "version": VERSION,
                        "transport": "mcp",
                        "endpoint": ENDPOINT,
                        "schema": "https://ucp.dev/2026-08-25/services/shopping/mcp.openrpc.json",
                    }
                ]
            },
            "capabilities": {name: [{"version": VERSION}] for name in READ_CAPABILITIES.values()},
        }
    }


@pytest.fixture
def metadata_network(
    monkeypatch: pytest.MonkeyPatch,
    gryphon_config: GryphonConfig,
) -> tuple[dict[str, Any], list[httpx.Request], list[NetworkClient], AsyncMock]:
    """Keep real URL/DNS/redirect validation while substituting fixture HTTP and MCP metadata."""
    documents: dict[str, Any] = {"/.well-known/ucp": profile(), "/custom/profile.json": profile()}
    requests: list[httpx.Request] = []
    clients: list[NetworkClient] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """Reject unplanned requests, including an attempted GET of the known-broken MCP entry."""
        requests.append(request)
        value = documents[request.url.path]
        return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)

    def factory(config: GryphonConfig) -> NetworkClient:
        """Create an owned pinned test client without shared credentials or cookies."""
        client = NetworkClient(
            config, resolver=AsyncMock(return_value=["93.184.216.34"]), transport=httpx.MockTransport(handle)
        )
        clients.append(client)
        return client

    discovery = AsyncMock(return_value=[tool(name) for name in READ_CAPABILITIES] + [tool("create_cart", allOf=[{}])])
    monkeypatch.setattr("gryphon.saas_spec_import.NetworkClient", factory)
    monkeypatch.setattr(ucp_discovery, "discover_tools", discovery)
    return documents, requests, clients, discovery


@pytest.mark.parametrize("path", ["/api/ucp/mcp", "/", "/.well-known/ucp", "/custom/profile.json"])
async def test_ucp_entry_inference_imports_all_six_reads_from_real_profile(
    gryphon_config: GryphonConfig,
    path: str,
    metadata_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient], AsyncMock],
) -> None:
    _, requests, clients, discovery = metadata_network
    result = await SpecImporter(gryphon_config, LIMIT).load("shop", url=ORIGIN + path, kind="ucp")
    expected_profile = ORIGIN + path if "profile.json" in path else PROFILE_URL
    assert result.source_url == ORIGIN + path
    assert result.resolved_profile_url == expected_profile and result.resolved_endpoint == ENDPOINT
    assert result.source_transport == "mcp"
    assert set(result.mcp_bindings) == set(READ_CAPABILITIES)
    assert result.diagnostics is not None and result.diagnostics.available_operations == 6
    assert result.diagnostics.unsupported_operations == 1
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].headers["Host"] == "merchant.example"
    assert requests[0].url.path == httpx.URL(expected_profile).path
    assert discovery.await_args is not None and discovery.await_args.args[1] == ENDPOINT
    assert all(client._client.is_closed for client in clients)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(301, headers={"Location": "https://evil.example/mcp"}),
        httpx.Response(404),
        httpx.Response(200, text="<html>not a profile</html>"),
    ],
)
async def test_explicit_mcp_fallback_never_follows_profile_redirect_or_html(
    gryphon_config: GryphonConfig,
    response: httpx.Response,
    metadata_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient], AsyncMock],
) -> None:
    documents, requests, _, discovery = metadata_network
    documents["/.well-known/ucp"] = response
    location = ORIGIN + "/api/ucp/mcp"
    imported = await SpecImporter(gryphon_config, LIMIT).load("shop", url=location, kind="ucp")
    assert imported.resolved_profile_url is None and imported.resolved_endpoint == location
    assert len(requests) == 1 and discovery.await_count == 1
    assert discovery.await_args is not None and discovery.await_args.args[1] == location
    assert any("No UCP profile" in warning for warning in imported.warnings)


@pytest.mark.parametrize("path", ["/", "/custom/profile.json"])
async def test_html_without_explicit_mcp_endpoint_never_discovers_tools(
    gryphon_config: GryphonConfig,
    path: str,
    metadata_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient], AsyncMock],
) -> None:
    documents, requests, clients, discovery = metadata_network
    for key in documents:
        documents[key] = httpx.Response(200, text='<html><a href="https://evil.example/mcp">MCP</a></html>')
    with pytest.raises(UCPImportError):
        await SpecImporter(gryphon_config, LIMIT).load("shop", url=ORIGIN + path, kind="ucp")
    assert len(requests) == 1 and all(client._client.is_closed for client in clients)
    discovery.assert_not_awaited()


async def test_malformed_actual_profile_does_not_fall_back_to_explicit_endpoint(
    gryphon_config: GryphonConfig,
    metadata_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient], AsyncMock],
) -> None:
    documents, _, _, discovery = metadata_network
    documents["/.well-known/ucp"]["ucp"]["version"] = "2099-01-01"
    with pytest.raises(UCPImportError):
        await SpecImporter(gryphon_config, LIMIT).load("shop", url=ORIGIN + "/mcp", kind="ucp")
    discovery.assert_not_awaited()


async def test_delegated_mcp_endpoint_still_requires_exact_operator_domain_policy(
    gryphon_config: GryphonConfig,
    metadata_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient], AsyncMock],
) -> None:
    _, _, _, discovery = metadata_network
    restricted = gryphon_config.model_copy(update={"allowed_domains": ["merchant.example"]})
    with pytest.raises(SecurityViolationError):
        await SpecImporter(restricted, LIMIT).load("shop", url=ORIGIN + "/mcp", kind="ucp")
    discovery.assert_not_awaited()


async def test_ucp_refresh_rediscovers_profile_and_mcp_metadata(
    gryphon_config: GryphonConfig,
    metadata_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient], AsyncMock],
) -> None:
    documents, requests, _, discovery = metadata_network
    importer = SpecImporter(gryphon_config, LIMIT)
    original = await importer.load("shop", url=ORIGIN + "/api/ucp/mcp", kind="ucp")
    documents["/.well-known/ucp"]["ucp"]["services"]["dev.ucp.shopping"][0]["endpoint"] = "https://new.example/mcp"
    refreshed = await importer.load("shop", url=original.source_url, kind="ucp")
    assert len(requests) == 2 and discovery.await_count == 2
    assert original.resolved_endpoint == ENDPOINT and refreshed.resolved_endpoint == "https://new.example/mcp"


async def test_ucp_discovery_cancellation_keeps_import_admission_until_cleanup(
    gryphon_config: GryphonConfig,
    metadata_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient], AsyncMock],
) -> None:
    _, _, clients, discovery = metadata_network
    entered = asyncio.Event()

    async def stalled(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Block the metadata-only MCP boundary so cancellation can be observed deterministically."""
        entered.set()
        await asyncio.Event().wait()
        return []

    discovery.side_effect = stalled
    importer = SpecImporter(gryphon_config, LIMIT)
    task = asyncio.create_task(importer.load("shop", url=ORIGIN + "/mcp", kind="ucp"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not importer._lock.locked() and all(client._client.is_closed for client in clients)


@pytest.mark.parametrize(
    "replacement",
    [{"ucp": None}, {"ucp": {"version": VERSION, "services": []}}, {"ucp": {"version": VERSION, "services": {}}}, {}],
)
async def test_invalid_explicit_profile_json_never_probes_tools(
    gryphon_config: GryphonConfig,
    replacement: dict[str, Any],
    metadata_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient], AsyncMock],
) -> None:
    documents, _, _, discovery = metadata_network
    if isinstance(replacement.get("ucp"), dict):
        replacement["ucp"]["capabilities"] = profile()["ucp"]["capabilities"]
    documents["/custom/profile.json"] = replacement
    with pytest.raises(UCPImportError):
        await SpecImporter(gryphon_config, LIMIT).load("shop", url=ORIGIN + "/custom/profile.json", kind="ucp")
    discovery.assert_not_awaited()


async def test_mcp_discovery_failure_is_actionable_without_upstream_text(
    gryphon_config: GryphonConfig,
    metadata_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient], AsyncMock],
) -> None:
    _, _, clients, discovery = metadata_network
    discovery.side_effect = ExecutionError("secret upstream detail")
    with pytest.raises(UCPImportError, match="initialize and tools/list") as error:
        await SpecImporter(gryphon_config, LIMIT).load("shop", url=ORIGIN + "/mcp", kind="ucp")
    assert "secret" not in str(error.value) and all(client._client.is_closed for client in clients)


async def test_exhausted_profile_budget_cannot_start_mcp_discovery(
    gryphon_config: GryphonConfig,
    metadata_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient], AsyncMock],
) -> None:
    _, _, _, discovery = metadata_network
    with pytest.raises(UCPImportError, match="byte budget"):
        await ucp_discovery._mcp(gryphon_config, ENDPOINT, None, None, 0)
    discovery.assert_not_awaited()


@pytest.mark.parametrize("version", ["2026-01-11", VERSION])
async def test_matching_rest_binding_remains_preferred(
    gryphon_config: GryphonConfig,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    metadata_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient], AsyncMock],
) -> None:
    documents, _, _, discovery = metadata_network
    advertised = profile()
    advertised["ucp"]["version"] = version
    rest = {"version": version, "endpoint": "https://merchant.example/rest", "schema": "https://ucp.dev/rest.json"}
    if version == "2026-01-11":
        advertised["ucp"]["services"]["dev.ucp.shopping"] = {"version": version, "rest": rest, "mcp": rest}
        advertised["ucp"]["capabilities"] = [{"name": "dev.ucp.shopping.checkout", "version": version}]
    else:
        advertised["ucp"]["services"]["dev.ucp.shopping"].append(rest | {"transport": "rest"})
    documents["/.well-known/ucp"] = advertised
    adapted = {
        "openapi": "3.1.0",
        "info": {"title": "REST", "version": version},
        "servers": [{"url": rest["endpoint"]}],
        "paths": {"/cart": {"get": {"operationId": "get_cart"}}},
        "x-gryphon-ucp": {"warnings": []},
    }
    adapt = AsyncMock(return_value=adapted)
    monkeypatch.setattr(ucp_discovery, "profile_to_openapi", adapt)
    result = await SpecImporter(gryphon_config, LIMIT).load("shop", url=ORIGIN, kind="ucp")
    assert result.source_transport == "rest" and result.resolved_endpoint == rest["endpoint"]
    adapt.assert_awaited_once()
    discovery.assert_not_awaited()
