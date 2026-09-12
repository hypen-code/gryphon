"""URL specification ingestion exercises the real pinned transport with synthetic HTTP and DNS."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gryphon.errors import CapacityError, CompileError, InputValidationError, SecurityViolationError
from gryphon.saas_spec_import import SpecImporter, source_url
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "API", "version": "1"},
    "servers": [{"url": "/api"}],
    "paths": {
        "/data": {
            "get": {"operationId": "data", "responses": {"200": {"description": "OK"}}},
            "post": {"operationId": "create", "responses": {"200": {"description": "OK"}}},
        }
    },
}


async def test_remote_import_pins_network_and_keeps_only_document_provenance(gryphon_config: GryphonConfig) -> None:
    """Retain safe provenance and compile GET-only diagnostics through pinned synthetic HTTP."""
    seen = []

    async def transport(request: httpx.Request) -> httpx.Response:
        """Record only isolated fixture traffic without contacting an upstream."""
        seen.append(request)
        return httpx.Response(200, json=SPEC, headers={"set-cookie": "private=not-forwarded"})

    resolver = AsyncMock(return_value=["93.184.216.34"])
    client = NetworkClient(gryphon_config, resolver=resolver, transport=httpx.MockTransport(transport))
    with patch("gryphon.saas_spec_import.NetworkClient", return_value=client):
        result = await SpecImporter(gryphon_config, 8192).load("api", url="https://example.com/spec.yaml")
    assert result.document["servers"] == [{"url": "https://example.com/api"}]
    assert result.source_url == "https://example.com/spec.yaml" and result.source_type == "openapi_url"
    assert result.diagnostics is not None
    assert result.diagnostics.model_dump() == {
        "total_operations": 2,
        "available_operations": 1,
        "filtered_operations": 1,
        "unsupported_operations": 0,
    }
    assert len(seen) == 1 and seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "example.com" and seen[0].extensions["sni_hostname"] == "example.com"
    assert "authorization" not in seen[0].headers and "cookie" not in seen[0].headers
    assert client._client.is_closed and not client._client.cookies


@pytest.mark.parametrize(
    "url",
    [
        "/etc/passwd",
        "file:///tmp/spec.yaml",
        "ftp://example.com/spec",
        "https://user:password@example.com/spec",
        "https://example.com/spec?token=private",
        "https://example.com/spec#fragment",
        "https://example.com/${TOKEN}",
        "https://example.com/" + "x" * 2048,
        None,
        42,
    ],
)
def test_spec_source_url_rejects_local_paths_secrets_and_ambiguous_sources(url: object) -> None:
    """Reject unsupported schemes, ambiguous sources and credential-bearing URLs."""
    with pytest.raises((InputValidationError, SecurityViolationError)):
        source_url(url)


def test_ucp_root_url_resolves_well_known_profile_without_rewriting_explicit_paths() -> None:
    """Only a bare HTTPS shop origin receives the standard profile suffix."""
    assert source_url("https://shop.example", ucp=True) == "https://shop.example/.well-known/ucp"
    assert source_url("https://shop.example/custom/profile", ucp=True) == "https://shop.example/custom/profile"


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["169.254.169.254"], ["93.184.216.34", "10.0.0.1"]])
async def test_spec_import_blocks_private_metadata_and_mixed_dns(
    gryphon_config: GryphonConfig, addresses: list[str]
) -> None:
    """Reject every unsafe DNS answer before dispatch and close the owned client."""
    transport = AsyncMock()
    client = NetworkClient(gryphon_config, resolver=AsyncMock(return_value=addresses), transport=transport)
    with patch("gryphon.saas_spec_import.NetworkClient", return_value=client), pytest.raises(SecurityViolationError):
        await SpecImporter(gryphon_config, 8192).load("api", url="https://example.com/spec")
    transport.handle_async_request.assert_not_awaited()
    assert client._client.is_closed


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://private.example/spec"}),
        httpx.Response(200, content=b"x" * 8193),
        httpx.Response(200, content=b"\xff"),
        httpx.Response(200, stream=httpx.ByteStream(b"{}"), headers={"content-encoding": "gzip"}),
        httpx.Response(503, content=b"private upstream detail"),
    ],
)
async def test_remote_fetch_failure_is_bounded_static_and_closes_client(
    gryphon_config: GryphonConfig, response: httpx.Response
) -> None:
    """Unsafe responses fail with static diagnostics and no redirect or resource leak."""
    requests = []

    def transport(request: httpx.Request) -> httpx.Response:
        """Return one controlled failure instead of using a live network."""
        requests.append(request)
        return response

    client = NetworkClient(
        gryphon_config, resolver=AsyncMock(return_value=["93.184.216.34"]), transport=httpx.MockTransport(transport)
    )
    with patch("gryphon.saas_spec_import.NetworkClient", return_value=client), pytest.raises(CompileError) as error:
        await SpecImporter(gryphon_config, 8192).load("api", url="https://example.com/spec")
    assert "private" not in str(error.value) and len(requests) == 1 and client._client.is_closed


async def test_remote_specs_still_reject_external_refs_and_interpolation(gryphon_config: GryphonConfig) -> None:
    """Remote provenance never grants external-reference or environment authority."""
    for extra in [{"$ref": "https://other.example/schema"}, {"description": "${SECRET}"}]:
        with (
            patch(
                "gryphon.saas_spec_import.NetworkClient.request",
                AsyncMock(return_value=httpx.Response(200, json=SPEC | {"x": extra})),
            ),
            pytest.raises(CompileError),
        ):
            await SpecImporter(gryphon_config, 8192).load("api", url="https://example.com/spec")


async def test_import_admission_and_cancellation_release_owned_resources(gryphon_config: GryphonConfig) -> None:
    """One in-flight import holds admission until cancellation has joined owned work."""
    entered = asyncio.Event()
    finalized = asyncio.Event()

    async def stalled(*args: object, **kwargs: object) -> httpx.Response:
        """Expose deterministic request entry and finalization barriers."""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()
        return httpx.Response(200, json=SPEC)

    importer = SpecImporter(gryphon_config, 8192)
    with patch("gryphon.saas_spec_import.NetworkClient.request", side_effect=stalled):
        task = asyncio.create_task(importer.load("api", url="https://example.com/spec"))
        try:
            async with asyncio.timeout(2):
                await entered.wait()
                with pytest.raises(CapacityError):
                    await importer.load("other", content=json.dumps(SPEC))
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert finalized.is_set() and not importer._lock.locked()


@pytest.mark.parametrize(
    "arguments",
    [
        {"content": {}},
        {"content": "{}", "kind": "ucp"},
        {"kind": []},
        {"url": "https://example.com", "content": "{}"},
        {"url": "https://example.com", "kind": "unknown"},
    ],
)
async def test_import_invalid_source_combinations_fail_closed(
    gryphon_config: GryphonConfig, arguments: dict[str, object]
) -> None:
    """Malformed source combinations fail before constructing any network client."""
    with patch("gryphon.saas_spec_import.NetworkClient") as network, pytest.raises(InputValidationError):
        await SpecImporter(gryphon_config, 8192).load("api", **arguments)
    network.assert_not_called()


@pytest.mark.parametrize("kind", [None, False, 1, [], {}, ["openapi"]])
async def test_import_invalid_kind_types_denied_before_network(gryphon_config: GryphonConfig, kind: object) -> None:
    """Unhashable and coercible JSON kinds never escape the closed import enum."""
    with patch("gryphon.saas_spec_import.NetworkClient") as network, pytest.raises(InputValidationError):
        await SpecImporter(gryphon_config, 8192).load("api", url="https://example.com/spec", kind=kind)
    network.assert_not_called()


@pytest.mark.parametrize("url", ["http://shop.example", "http://shop.example/profile"])
async def test_ucp_http_profile_denied_before_network(gryphon_config: GryphonConfig, url: str) -> None:
    """Both root and explicit UCP profile URLs require HTTPS before fetching."""
    with patch("gryphon.saas_spec_import.NetworkClient") as network, pytest.raises(InputValidationError):
        await SpecImporter(gryphon_config, 8192).load("shop", url=url, kind="ucp")
    network.assert_not_called()


async def test_import_repeated_cancellation_joins_network_close(gryphon_config: GryphonConfig) -> None:
    """Repeated cancellation cannot detach finish_cleanup or release admission before close."""
    closing, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    client = NetworkClient(gryphon_config)
    original_close = client.close

    async def stalled_close() -> None:
        """Block owned cleanup until the test releases the underlying real client close."""
        closing.set()
        await release.wait()
        await original_close()
        closed.set()

    importer = SpecImporter(gryphon_config, 8192)
    with (
        patch("gryphon.saas_spec_import.NetworkClient", return_value=client),
        patch.object(client, "request", AsyncMock(return_value=httpx.Response(200, json=SPEC))),
        patch.object(client, "close", side_effect=stalled_close) as close,
    ):
        task = asyncio.create_task(importer.load("api", url="https://example.com/spec"))
        try:
            async with asyncio.timeout(2):
                await closing.wait()
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done() and not closed.is_set() and importer._lock.locked()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await original_close()
    close.assert_awaited_once()
    assert closed.is_set() and client._client.is_closed and not importer._lock.locked()
