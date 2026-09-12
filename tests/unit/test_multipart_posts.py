"""Scalar-only multipart contracts and fully mocked broker HTTP regressions."""

from __future__ import annotations

import ast
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx
import pytest

from gryphon.compiler.codegen import CodeGenerator
from gryphon.compiler.orchestrator import Orchestrator
from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.errors import CompileError, ExecutionError, InputValidationError, SecurityViolationError
from gryphon.models import ExecutionScope, ParamSchema, ReadOnlyPostOperation, SwaggerSource
from gryphon.runtime.registry import Registry
from gryphon.security.broker import ToolBroker
from gryphon.security.form_encoding import encode_multipart
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from gryphon.config import GryphonConfig

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "cse_read_only_posts.yaml"
_MULTIPART = "multipart/form-data"


@pytest.fixture
async def multipart_broker(
    gryphon_config: GryphonConfig,
) -> AsyncIterator[tuple[ToolBroker, Registry, list[httpx.Request]]]:
    """Compile a synthetic source and capture pinned requests without any live networking."""
    gryphon_config.swaggers = [SwaggerSource(name="cse_api", swagger_url=str(_FIXTURE), is_read_only=True)]
    gryphon_config.allowed_read_only_post_operations = [
        ReadOnlyPostOperation(server_name="cse_api", base_url="https://market.example/api", path=path)
        for path in ("/companyProfile", "/companyInfoVideo")
    ]
    assert not (await Orchestrator(gryphon_config).compile_all()).failed
    registry = Registry(gryphon_config.compiled_output_dir)
    registry.load()
    requests: list[httpx.Request] = []

    async def resolver(host: str, port: int) -> list[str]:
        """Provide a synthetic DNS answer."""
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        """Return synthetic JSON and capture exact bytes."""
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
    """Give a request a finite deadline without consulting wall-clock time."""
    return ExecutionScope(run_id="multipart", deadline=10**12)


@pytest.mark.parametrize(
    "function,path", [("get_company_profile", "/companyProfile"), ("get_company_info_video", "/companyInfoVideo")]
)
async def test_multipart_broker_emits_literal_text_not_files(
    multipart_broker: tuple[ToolBroker, Registry, list[httpx.Request]], function: str, path: str
) -> None:
    broker, registry, requests = multipart_broker
    symbol = '/nonexistent/private-file.txt\r\nX-Injected: yes\r\n"é &+=/'
    with patch("builtins.open", side_effect=AssertionError("Unexpected file access")):
        assert await broker.invoke("cse_api", function, {"json_body": {"symbol": symbol}}, _scope()) == {
            "synthetic": True
        }
    request = requests[0]
    assert request.method == "POST" and request.url.path == "/api" + path
    assert request.headers["Host"] == "market.example"
    assert "X-Injected" not in request.headers and "filename=" not in request.content.decode()
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + request.headers["Content-Type"].encode() + b"\r\n\r\n" + request.content
    )
    boundary = message.get_boundary()
    assert boundary is not None and len(boundary) == 48
    assert request.content.startswith(b"--" + boundary.encode() + b"\r\n")
    assert request.content.endswith(b"--" + boundary.encode() + b"--\r\n")
    parts = list(message.iter_parts())
    assert len(parts) == 1 and parts[0].get_filename() is None
    assert parts[0].get_param("name", header="Content-Disposition") == "symbol"
    assert parts[0].get_payload(decode=True) == symbol.encode()
    assert "X-Injected" not in parts[0]
    assert registry.get_endpoint("cse_api", function).request_body_media_type == _MULTIPART


async def test_multipart_broker_repeats_scalar_arrays(
    multipart_broker: tuple[ToolBroker, Registry, list[httpx.Request]],
) -> None:
    broker, _, requests = multipart_broker
    body = {"symbol": "SYN", "active": True, "limit": 0, "weight": 1.5, "categories": ["a", "b"]}
    await broker.invoke("cse_api", "get_company_profile", {"json_body": body}, _scope())
    wire = requests[0].content
    assert wire.count(b'name="categories"') == 2
    assert b"\r\n\r\ntrue\r\n" in wire and b"\r\n\r\n0\r\n" in wire and b"\r\n\r\n1.5\r\n" in wire
    assert not wire.startswith(b"{") and not wire.startswith(b"symbol=")


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"json_body": {}},
        {"json_body": None},
        {"json_body": {"symbol": {"filename": "/private"}}},
        {"json_body": {"symbol": "SYN", "Content-Type": "multipart/form-data; boundary=evil"}},
        {"json_body": {"symbol": "SYN"}, "Content-Type": "evil"},
    ],
)
async def test_multipart_invalid_inputs_cannot_dispatch(
    multipart_broker: tuple[ToolBroker, Registry, list[httpx.Request]], arguments: dict[str, Any]
) -> None:
    broker, _, requests = multipart_broker
    with pytest.raises(InputValidationError):
        await broker.invoke("cse_api", "get_company_profile", arguments, _scope())
    assert requests == []


async def test_multipart_declared_header_cannot_select_boundary(
    multipart_broker: tuple[ToolBroker, Registry, list[httpx.Request]],
) -> None:
    broker, registry, requests = multipart_broker
    endpoint = registry.get_endpoint("cse_api", "get_company_profile").model_copy(deep=True)
    endpoint.parameters.append(ParamSchema(name="Content-Type", location="header", param_type="string"))
    endpoint.input_schema["properties"]["Content-Type"] = {"type": "string"}
    arguments = {"json_body": {"symbol": "SYN"}, "Content-Type": "multipart/form-data; boundary=evil"}
    with patch.object(registry, "get_endpoint", return_value=endpoint), pytest.raises(SecurityViolationError):
        await broker.invoke("cse_api", "get_company_profile", arguments, _scope())
    assert not requests


async def test_multipart_revoked_route_cannot_dispatch(
    multipart_broker: tuple[ToolBroker, Registry, list[httpx.Request]],
) -> None:
    broker, _, requests = multipart_broker
    broker._config.allowed_read_only_post_operations = []
    with pytest.raises(SecurityViolationError):
        await broker.invoke("cse_api", "get_company_profile", {"json_body": {"symbol": "SYN"}}, _scope())
    assert not requests


@pytest.mark.parametrize(
    "field",
    [
        {"type": "string", "format": "binary"},
        {"type": "string", "format": "byte"},
        {"type": "file"},
        {"type": "array", "items": {"type": "string", "format": "binary"}},
        {"type": "object"},
        {"type": "array", "items": {"type": "object"}},
        {"type": ["string", "null"]},
    ],
)
def test_multipart_compiler_rejects_binary_nested_or_nullable_fields(field: dict[str, Any]) -> None:
    parser = SwaggerParser(SwaggerSource(name="test", swagger_url="unused"))
    body = {"content": {_MULTIPART: {"schema": {"type": "object", "properties": {"symbol": field}}}}}
    with pytest.raises(CompileError):
        parser._parse_request_body(body)


@pytest.mark.parametrize("name", ['x"; filename="p', "x\r\nX-Header: yes", "é", "x" * 129])
def test_multipart_compiler_rejects_header_like_field_names(name: str) -> None:
    parser = SwaggerParser(SwaggerSource(name="test", swagger_url="unused"))
    body = {"content": {_MULTIPART: {"schema": {"type": "object", "properties": {name: {"type": "string"}}}}}}
    with pytest.raises(CompileError):
        parser._parse_request_body(body)


@pytest.mark.parametrize(
    "encoding",
    [
        {"symbol": {"contentType": "application/octet-stream"}},
        {"symbol": {"headers": {"X-Test": {}}}},
        {"symbol": {"explode": False}},
        {"symbol": {"style": "deepObject"}},
    ],
)
def test_multipart_compiler_rejects_part_encoding_overrides(encoding: dict[str, Any]) -> None:
    parser = SwaggerParser(SwaggerSource(name="test", swagger_url="unused"))
    with pytest.raises(CompileError):
        parser._parse_request_body({"content": {_MULTIPART: {"schema": {"type": "object"}, "encoding": encoding}}})


@pytest.mark.parametrize(
    "body",
    [
        None,
        "/host/path",
        {"x": b"bytes"},
        {"x": ("file", "content")},
        {"x": {"filename": "path"}},
        {"x": None},
        {"x": [[1]]},
        {1: "x"},
        {'x"\r\n': "value"},
        {"x": "\ud800"},
        {"x": ["a"] * 1025},
        {str(i): "a" for i in range(1025)},
    ],
)
def test_multipart_encoder_rejects_file_like_or_unbounded_shapes(body: Any) -> None:
    with pytest.raises(InputValidationError):
        encode_multipart(body)


@pytest.mark.parametrize("text", ["a" * (2 * 1024 * 1024 + 1), "€" * 700000, "a" * (2 * 1024 * 1024)])
def test_multipart_encoder_bounds_wire_bytes_including_framing(text: str) -> None:
    with pytest.raises(InputValidationError):
        encode_multipart({"x": text})


def test_multipart_boundary_is_fresh_and_collision_rejected() -> None:
    first, _ = encode_multipart({"x": "text"})
    second, _ = encode_multipart({"x": "text"})
    assert first != second
    with (
        patch("gryphon.security.form_encoding.secrets.token_hex", return_value="collision"),
        pytest.raises(InputValidationError),
    ):
        encode_multipart({"x": "text with collision"})


def test_multipart_empty_object_emits_valid_closing_boundary() -> None:
    content_type, content = encode_multipart({"empty": []})
    boundary = content_type.split("boundary=", 1)[1]
    assert content == f"--{boundary}--\r\n".encode()


async def test_multipart_network_rejects_ambiguous_json_or_form_payload(gryphon_config: GryphonConfig) -> None:
    network = NetworkClient(gryphon_config)
    try:
        payloads: list[dict[str, Any]] = [{"json_body": {}}, {"json_body_present": True}, {"data": {}}]
        for extra in payloads:
            with pytest.raises(ExecutionError, match="encoded safely"):
                await network.request("POST", "https://example.test/query", content=b"multipart", **extra)
    finally:
        await network.close()


async def test_multipart_network_refuses_file_like_raw_content(gryphon_config: GryphonConfig) -> None:
    network = NetworkClient(gryphon_config)
    options: dict[str, Any] = {"content": [b"not a file stream"]}
    try:
        with pytest.raises(ExecutionError, match="encoded safely"):
            await network.request("POST", "https://example.test/query", **options)
    finally:
        await network.close()


async def test_multipart_sdk_uses_shared_scalar_serializer(
    multipart_broker: tuple[ToolBroker, Registry, list[httpx.Request]],
) -> None:
    broker, _, _ = multipart_broker
    source = SwaggerSource(name="cse_api", swagger_url=str(_FIXTURE), is_read_only=True)
    spec = await SwaggerParser(source, config=broker._config).parse()
    code = CodeGenerator().generate(spec)
    tree = ast.parse(code)
    assert "multipart_body=True" in code and "encode_multipart" in code and "files=" not in code
    assert all(
        (node.end_lineno or node.lineno) - node.lineno < 50
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    )
    manifest = Orchestrator(broker._config)._build_manifest("cse_api", spec, "test")
    assert json.loads(manifest.model_dump_json())["endpoints"][1]["request_body_media_type"] == _MULTIPART
    assert len(manifest.endpoints) == 3


def cse_review_permits() -> list[ReadOnlyPostOperation]:
    """Return candidate permits for offline review of the supplied CSE document.

    This helper never installs configuration or makes calls. Route existence and
    encoding were verified by safe parsing, not upstream side-effect semantics.
    An operator must independently approve every route before production use.
    """
    paths = (
        "/marketStatus",
        "/marketSummery",
        "/aspiData",
        "/snpData",
        "/allSectors",
        "/dailyMarketSummery",
        "/topGainers",
        "/topLooses",
        "/mostActiveTrades",
        "/todaySharePrice",
        "/tradeSummary",
        "/detailedTrades",
        "/companyInfoSummery",
        "/companyChartDataByStock",
        "/getNewListingsRelatedNoticesAnnouncements",
        "/getBuyInBoardAnnouncements",
        "/approvedAnnouncement",
        "/getFinancialAnnouncement",
        "/circularAnnouncement",
        "/directiveAnnouncement",
        "/getNonComplianceAnnouncements",
        "/getCOVIDAnnouncements",
        "/companyProfile",
        "/companyInfoVideo",
        "/financials",
    )
    return [
        ReadOnlyPostOperation(server_name="cse_api", base_url="https://www.cse.lk/api", path=path) for path in paths
    ]


def test_cse_review_permits_are_exact_and_not_installed(gryphon_config: GryphonConfig) -> None:
    permits = cse_review_permits()
    assert len(permits) == len({permit.path for permit in permits}) == 25
    assert not gryphon_config.allowed_read_only_post_operations
