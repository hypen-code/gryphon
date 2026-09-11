"""Compiler document transport tests with mocked DNS and numeric HTTP transport only."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from gryphon.compiler import documents
from gryphon.compiler.orchestrator import Orchestrator
from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.config import GryphonConfig
from gryphon.errors import SwaggerFetchError
from gryphon.models import SwaggerSource
from gryphon.security.network import NetworkClient


def _install_network(
    monkeypatch: pytest.MonkeyPatch,
    content: bytes = b"document",
    status: int = 200,
    address: str | list[str] = "93.184.216.34",
    failure: bool = False,
) -> list[httpx.Request]:
    """Use real DNS-pinning policy with a deterministic resolver and mock numeric transport."""
    requests: list[httpx.Request] = []

    async def resolve(host: str, port: int) -> list[str]:
        """Resolve without issuing live DNS traffic."""
        return address if isinstance(address, list) else [address]

    def handle(request: httpx.Request) -> httpx.Response:
        """Capture only requests that passed host/address policy and DNS pinning."""
        requests.append(request)
        if failure:
            raise httpx.ConnectError("credential-in-error private-url", request=request)
        return httpx.Response(status, content=content, headers={"Location": "https://127.0.0.1/private"})

    def client(config: GryphonConfig) -> NetworkClient:
        """Preserve the passed configuration instead of replacing security policy in tests."""
        return NetworkClient(config, resolver=resolve, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(documents, "NetworkClient", client)
    return requests


# ---------------------------------------------------------------------------
# _fetch_remote — bounded DNS-pinned transport
# ---------------------------------------------------------------------------


async def test_fetch_remote_success(monkeypatch: pytest.MonkeyPatch, weather_swagger_source: SwaggerSource) -> None:
    """The same fixture parses over a verified-policy remote document transport."""
    content = Path(weather_swagger_source.swagger_url).read_bytes()
    requests = _install_network(monkeypatch, content)
    source = weather_swagger_source.model_copy(update={"swagger_url": "https://spec.example/spec"})
    assert (await SwaggerParser(source).parse()).name == "weather"
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["Host"] == "spec.example"
    assert requests[0].extensions["sni_hostname"] == "spec.example"


@pytest.mark.parametrize("status", [302, 404, 500])
async def test_remote_redirects_and_http_errors_rejected(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """A failed status produces one sanitized error, never a follow-up redirect request."""
    requests = _install_network(monkeypatch, status=status)
    with pytest.raises(SwaggerFetchError, match="securely"):
        await documents.fetch_remote("https://spec.example/spec")
    assert len(requests) == 1


async def test_remote_transport_errors_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport error details cannot disclose URLs or credentials."""
    _install_network(monkeypatch, failure=True)
    with pytest.raises(SwaggerFetchError) as error:
        await documents.fetch_remote("https://spec.example/spec")
    assert "private-url" not in str(error.value)
    assert "credential-in-error" not in str(error.value)
    assert error.value.__suppress_context__


async def test_remote_size_limit_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The configured byte cap reaches the pinned network client's bounded reader."""
    _install_network(monkeypatch, content=b"x" * 1025)
    config = GryphonConfig.model_construct(max_spec_size_bytes=1024)
    parser = SwaggerParser(SwaggerSource(name="test", swagger_url="https://spec.example/spec"), config=config)
    with pytest.raises(SwaggerFetchError, match="size limit"):
        await parser.parse()


@pytest.mark.parametrize("allowed", [False, True])
async def test_remote_private_network_optin_passed_explicitly(
    monkeypatch: pytest.MonkeyPatch,
    allowed: bool,
) -> None:
    """Private destinations require explicit host configuration even for specs and skills."""
    requests = _install_network(monkeypatch, address="127.0.0.1")
    config = GryphonConfig.model_construct(allow_private_networks=allowed)
    if allowed:
        assert await documents.fetch_remote("https://spec.example/spec", config=config) == "document"
    else:
        with pytest.raises(SwaggerFetchError):
            await documents.fetch_remote("https://spec.example/spec", config=config)
    assert len(requests) == int(allowed)


async def test_remote_domain_allowlist_enforced_for_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skills fetches use explicit configuration, not a permissive separate HTTP client."""
    requests = _install_network(monkeypatch)
    config = GryphonConfig.model_construct(allowed_domains=["approved.example"])
    assert await Orchestrator._fetch_skills_content("https://other.example/skills", "test", config=config) is None
    assert requests == []


async def test_spec_auth_and_extra_headers_not_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """API credentials never accompany external specification or description requests."""
    requests = _install_network(monkeypatch)
    source = SwaggerSource(
        name="test",
        swagger_url="https://spec.example/spec",
        auth_header="private-auth",
        extra_headers={"X-Key": "private-key"},
    )
    assert await SwaggerParser(source)._fetch_document() == "document"
    assert "Authorization" not in requests[0].headers and "X-Key" not in requests[0].headers


async def test_remote_credentials_in_url_rejected_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL userinfo cannot bypass the no-auth document transport contract."""
    requests = _install_network(monkeypatch)
    with pytest.raises(SwaggerFetchError) as error:
        await documents.fetch_remote("https://private-user:private-password@spec.example/spec")
    assert "private-password" not in str(error.value)
    assert requests == []


async def test_github_blob_conversion_obeys_raw_host_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raw GitHub conversion still validates the resulting destination hostname."""
    requests = _install_network(monkeypatch)
    config = GryphonConfig.model_construct(allowed_domains=["raw.githubusercontent.com"])
    await documents.fetch_remote("https://github.com/owner/repo/blob/main/spec.yaml", config=config)
    assert requests[0].headers["Host"] == "raw.githubusercontent.com"


async def test_local_missing_oversized_and_symlink_documents_rejected(tmp_path: Path) -> None:
    """Local trusted paths remain bounded and do not permit symlink traversal."""
    with pytest.raises(SwaggerFetchError):
        documents.fetch_local(str(tmp_path / "missing"))
    path = tmp_path / "document"
    path.write_bytes(b"x" * 16)
    with pytest.raises(SwaggerFetchError, match="size"):
        documents.fetch_local(str(path), max_bytes=8)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(SwaggerFetchError, match="Symlink"):
        documents.fetch_local(str(alias))


@pytest.mark.parametrize("address", ["169.254.169.254", ["93.184.216.34", "127.0.0.1"]])
async def test_remote_metadata_or_mixed_private_dns_rejected(
    monkeypatch: pytest.MonkeyPatch,
    address: str | list[str],
) -> None:
    """Every DNS answer is checked before connecting, including hidden private alternatives."""
    requests = _install_network(monkeypatch, address=address)
    with pytest.raises(SwaggerFetchError):
        await documents.fetch_remote("https://spec.example/spec")
    assert requests == []


async def test_orchestrator_passes_explicit_document_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    weather_swagger_source: SwaggerSource,
) -> None:
    """A compile source inherits its application allowlist, never permissive fetch defaults."""
    requests = _install_network(monkeypatch, Path(weather_swagger_source.swagger_url).read_bytes())
    source = weather_swagger_source.model_copy(update={"swagger_url": "https://spec.example/spec"})
    config = GryphonConfig.model_construct(allowed_domains=["approved.example"], compiled_output_dir=str(tmp_path))
    with pytest.raises(SwaggerFetchError):
        await Orchestrator(config)._compile_source(source, True)
    assert requests == []
