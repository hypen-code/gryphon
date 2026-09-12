"""Offline regressions for CSE-shaped read-only POST and form-urlencoded contracts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import ValidationError

from gryphon.compiler.catalog import load_manifest
from gryphon.compiler.orchestrator import Orchestrator
from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.config import GryphonConfig
from gryphon.errors import CompileError, InputValidationError, SecurityViolationError
from gryphon.models import ExecutionScope, ReadOnlyPostOperation, SwaggerSource
from gryphon.runtime.execution_results import fingerprint
from gryphon.runtime.registry import Registry
from gryphon.security.broker import ToolBroker
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "cse_read_only_posts.yaml"
_BASE = "https://market.example/api"


def _permit(path: str = "/companyInfoSummery", **updates: str) -> ReadOnlyPostOperation:
    """Build an operator-owned route attestation independent of the uploaded document."""
    return ReadOnlyPostOperation.model_validate({"server_name": "cse_api", "base_url": _BASE, "path": path, **updates})


def _source(**updates: Any) -> SwaggerSource:
    """Use a synthetic local document, never the actual CSE service."""
    return SwaggerSource.model_validate(
        {"name": "cse_api", "swagger_url": str(_FIXTURE), "is_read_only": True, **updates}
    )


@pytest.fixture
async def cse_broker(gryphon_config: GryphonConfig) -> AsyncIterator[tuple[ToolBroker, Registry, list[httpx.Request]]]:
    """Exercise compilation, manifest loading and broker dispatch with fully mocked DNS/HTTP."""
    gryphon_config.swaggers = [_source()]
    gryphon_config.allowed_read_only_post_operations = [_permit(), _permit("/marketStatus")]
    result = await Orchestrator(gryphon_config).compile_all()
    assert not result.failed
    registry = Registry(gryphon_config.compiled_output_dir)
    registry.load()
    requests: list[httpx.Request] = []

    async def resolver(host: str, port: int) -> list[str]:
        """Never resolve a real name."""
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        """Accept only fixture traffic through the injected transport."""
        requests.append(request)
        return httpx.Response(200, json={"synthetic": True})

    broker = ToolBroker(gryphon_config, registry, allow_environment=False)
    await broker._network.close()
    broker._network = NetworkClient(gryphon_config, resolver=resolver, transport=httpx.MockTransport(handler))
    try:
        yield broker, registry, requests
    finally:
        await broker.close()


def _scope() -> ExecutionScope:
    """Issue a fresh broker-owned execution budget."""
    return ExecutionScope(run_id="synthetic", deadline=time.monotonic() + 30)


async def test_cse_default_read_only_filter_retains_only_get(gryphon_config: GryphonConfig) -> None:
    spec = await SwaggerParser(_source(), config=gryphon_config).parse()
    assert [(ep.method, ep.path) for ep in spec.endpoints] == [("GET", "/allSecurityCode")]


async def test_cse_exact_permits_include_only_reviewed_posts(gryphon_config: GryphonConfig) -> None:
    gryphon_config.allowed_read_only_post_operations = [_permit(), _permit("/marketStatus")]
    spec = await SwaggerParser(_source(), config=gryphon_config).parse()
    assert len(spec.endpoints) == 3
    assert {ep.path for ep in spec.endpoints if ep.read_only_post} == {"/companyInfoSummery", "/marketStatus"}
    assert all(ep.method in {"GET", "POST"} for ep in spec.endpoints)
    gryphon_config.swaggers = [_source()]
    result = await Orchestrator(gryphon_config).compile_all(dry_run=True)
    assert not result.failed and result.total_endpoints == 3
    assert not Path(gryphon_config.compiled_output_dir).exists()


@pytest.mark.parametrize(
    "change", [{"server_name": "other"}, {"base_url": "https://other.example/api"}, {"path": "/companyInfo"}]
)
async def test_cse_permits_do_not_match_other_authority(gryphon_config: GryphonConfig, change: dict[str, str]) -> None:
    gryphon_config.allowed_read_only_post_operations = [_permit(**change)]
    spec = await SwaggerParser(_source(), config=gryphon_config).parse()
    assert len(spec.endpoints) == 1


async def test_cse_operation_server_override_cannot_reuse_permit(gryphon_config: GryphonConfig) -> None:
    gryphon_config.allowed_read_only_post_operations = [_permit()]
    parser = SwaggerParser(_source(), config=gryphon_config)
    parser._raw_doc = parser._load_document(_FIXTURE.read_text())
    parser._raw_doc["paths"]["/companyInfoSummery"]["post"]["servers"] = [{"url": "https://other.example/api"}]
    assert len(parser._parse_paths()) == 1
    parser._source.base_url = _BASE
    assert len(parser._parse_paths()) == 2


async def test_cse_form_body_is_native_inspectable_contract(
    cse_broker: tuple[ToolBroker, Registry, list[httpx.Request]],
) -> None:
    _, registry, _ = cse_broker
    endpoint = registry.get_endpoint("cse_api", "get_company_info_summery")
    assert endpoint.request_body_media_type == "application/x-www-form-urlencoded"
    assert endpoint.input_schema["required"] == ["json_body"]
    assert endpoint.input_schema["properties"]["json_body"]["required"] == ["symbol"]
    assert endpoint.input_schema["properties"]["json_body"]["additionalProperties"] is False
    assert endpoint.read_only_post


async def test_cse_form_dispatch_encodes_wire_values(
    cse_broker: tuple[ToolBroker, Registry, list[httpx.Request]],
) -> None:
    broker, _, requests = cse_broker
    body = {"symbol": "SYN &+=/é", "active": False, "limit": 2, "weight": 1.5, "categories": ["a b", "c&d"]}
    assert await broker.invoke("cse_api", "get_company_info_summery", {"json_body": body}, _scope()) == {
        "synthetic": True
    }
    request = requests[0]
    assert request.method == "POST" and request.url.path == "/api/companyInfoSummery"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert request.headers["Host"] == "market.example"
    assert request.extensions["sni_hostname"] == "market.example"
    assert (
        request.content
        == b"symbol=SYN+%26%2B%3D%2F%C3%A9&active=false&limit=2&weight=1.5&categories=a+b&categories=c%26d"
    )
    assert not broker._config.allow_writes


@pytest.mark.parametrize("arguments", [{}, {"json_body": {}}])
async def test_cse_optional_empty_form_is_not_json(
    cse_broker: tuple[ToolBroker, Registry, list[httpx.Request]], arguments: dict[str, Any]
) -> None:
    broker, _, requests = cse_broker
    await broker.invoke("cse_api", "get_market_status", arguments, _scope())
    assert requests[0].content == b""
    assert requests[0].headers["Content-Type"] == "application/x-www-form-urlencoded"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"json_body": {}},
        {"json_body": None},
        {"json_body": {"symbol": "SYN", "url": "bad"}},
        {"json_body": {"symbol": 2}},
        {"json_body": {"symbol": "SYN", "categories": [None]}},
        {"json_body": {"symbol": "SYN"}, "approval": True},
    ],
)
async def test_cse_invalid_form_inputs_never_dispatch(
    cse_broker: tuple[ToolBroker, Registry, list[httpx.Request]], arguments: dict[str, Any]
) -> None:
    broker, _, requests = cse_broker
    with pytest.raises(InputValidationError):
        await broker.invoke("cse_api", "get_company_info_summery", arguments, _scope())
    assert not requests


async def test_cse_revoked_permit_blocks_loaded_manifest(
    cse_broker: tuple[ToolBroker, Registry, list[httpx.Request]],
) -> None:
    broker, registry, requests = cse_broker
    previous = fingerprint(broker._config, registry)
    broker._config.allowed_read_only_post_operations = []
    assert fingerprint(broker._config, registry) != previous
    broker._config.allow_writes = True
    broker._config.allowed_write_operations = ["cse_api.get_market_status"]
    with pytest.raises(SecurityViolationError, match="Read-only POST"):
        await broker.invoke("cse_api", "get_market_status", {}, _scope())
    assert not requests


async def test_cse_permit_rechecked_after_auth_await(
    cse_broker: tuple[ToolBroker, Registry, list[httpx.Request]],
) -> None:
    broker, _, requests = cse_broker

    async def revoke(*args: object) -> dict[str, str]:
        """Revoke live operator authority during credential resolution."""
        broker._config.allowed_read_only_post_operations = []
        return {}

    with patch.object(broker._vault, "resolve", AsyncMock(side_effect=revoke)), pytest.raises(SecurityViolationError):
        await broker.invoke("cse_api", "get_market_status", {}, _scope())
    assert not requests


@pytest.mark.parametrize("change", [{"method": "PUT"}, {"path": "/update"}, {"base_url": "https://other.example/api"}])
async def test_cse_forged_manifest_classification_cannot_grant_access(
    cse_broker: tuple[ToolBroker, Registry, list[httpx.Request]], change: dict[str, str]
) -> None:
    broker, registry, requests = cse_broker
    endpoint = registry.get_endpoint("cse_api", "get_market_status")
    forged = endpoint.model_copy(update=change)
    manifest = registry.get_manifest("cse_api")
    with pytest.raises(SecurityViolationError):
        broker._authorize(manifest, forged, "cse_api", forged.function_name)
    assert not requests


async def test_cse_unpermitted_post_still_requires_write_controls(gryphon_config: GryphonConfig) -> None:
    spec = await SwaggerParser(_source(is_read_only=False), config=gryphon_config).parse()
    manifest = Orchestrator(gryphon_config)._build_manifest("cse_api", spec, "test")
    endpoint = next(ep for ep in manifest.endpoints if ep.path == "/update")
    broker = ToolBroker(gryphon_config, Registry(gryphon_config.compiled_output_dir), allow_environment=False)
    try:
        with pytest.raises(SecurityViolationError):
            broker._authorize(manifest, endpoint, "cse_api", endpoint.function_name)
        gryphon_config.allow_writes = True
        with pytest.raises(SecurityViolationError):
            broker._authorize(manifest, endpoint, "cse_api", endpoint.function_name)
        gryphon_config.allowed_write_operations = [f"cse_api.{endpoint.function_name}"]
        broker._authorize(manifest, endpoint, "cse_api", endpoint.function_name)
        manifest.is_read_only = True
        with pytest.raises(SecurityViolationError):
            broker._authorize(manifest, endpoint, "cse_api", endpoint.function_name)
    finally:
        await broker.close()


@pytest.mark.parametrize(
    "updates",
    [
        {"server_name": "*"},
        {"server_name": "class"},
        {"base_url": "https://user:pass@example.test"},
        {"base_url": "https://example.test:invalid"},
        {"base_url": "https://*.example.test"},
        {"base_url": "https://example.test/?q=x"},
        {"base_url": "https://example.test/../api"},
        {"base_url": "https://example.test/{server}"},
        {"path": "/{operation}"},
        {"path": "/*"},
        {"path": "/%2e%2e"},
        {"path": "/../write"},
        {"path": "//write"},
        {"method": "DELETE"},
        {"operationId": "getMarketStatus"},
    ],
)
def test_cse_operator_permits_reject_ambiguous_scope(updates: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        _permit(**updates)


def test_cse_operator_permits_load_from_explicit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    value = json.dumps([_permit().model_dump()])
    monkeypatch.setenv("GRYPHON_ALLOWED_READ_ONLY_POST_OPERATIONS", value)
    options: dict[str, Any] = {"_env_file": None}
    config = GryphonConfig(**options)
    assert config.allowed_read_only_post_operations == [_permit()]
    assert not config.allow_writes


async def test_cse_compile_identity_changes_with_permits(gryphon_config: GryphonConfig) -> None:
    compiler = Orchestrator(gryphon_config)
    before = compiler._source_hash(_source())
    gryphon_config.allowed_read_only_post_operations = [_permit()]
    assert compiler._source_hash(_source()) != before


async def test_cse_equivalent_permit_sets_preserve_identity(
    cse_broker: tuple[ToolBroker, Registry, list[httpx.Request]],
) -> None:
    broker, registry, _ = cse_broker
    previous = fingerprint(broker._config, registry)
    broker._config.allowed_read_only_post_operations = [_permit("/marketStatus"), _permit(), _permit()]
    assert fingerprint(broker._config, registry) == previous


async def test_cse_manifest_rejects_read_only_classification_on_put(
    cse_broker: tuple[ToolBroker, Registry, list[httpx.Request]], gryphon_config: GryphonConfig
) -> None:
    path = Path(gryphon_config.compiled_output_dir) / "cse_api" / "manifest.json"
    raw = json.loads(path.read_text())
    endpoint = next(ep for ep in raw["endpoints"] if ep["read_only_post"])
    endpoint["method"] = "PUT"
    path.write_text(json.dumps(raw))
    with pytest.raises(CompileError):
        load_manifest(path)
