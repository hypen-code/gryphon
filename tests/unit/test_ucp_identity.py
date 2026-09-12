"""Public HTTPS UCP identity validation and schema-limited default regression tests."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError
from test_ucp_mcp import tool

from gryphon.compiler.ucp_mcp import native_schema
from gryphon.config import GryphonConfig
from gryphon.errors import ExecutionError, InputValidationError, UpstreamDiagnosticError
from gryphon.models import EndpointManifest, MCPBinding
from gryphon.security.encoding import validate_arguments
from gryphon.security.ucp_identity import (
    configured_profile,
    effective_profile,
    prepare_arguments,
    requires_profile,
    validate_profile,
    validate_profile_addresses,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

PROFILE = "https://operator.example/agent.json"
CALLER = "https://caller.example/agent.json"


@pytest.fixture
def endpoint() -> EndpointManifest:
    """Keep format-only profile schema, reproducing the original empty-string hole."""
    schema = native_schema(tool()["inputSchema"])
    return EndpointManifest(
        function_name="get_cart",
        summary="Cart",
        method="POST",
        path="/__mcp__/get_cart",
        parameters_summary="",
        response_summary="",
        request_body_schema=schema,
        input_schema={
            "type": "object",
            "properties": {"json_body": schema},
            "required": ["json_body"],
            "additionalProperties": False,
        },
        mcp_binding=MCPBinding(
            endpoint="https://merchant.example/mcp", tool_name="get_cart", tool_fingerprint="a" * 64
        ),
    )


def _config(**updates: Any) -> GryphonConfig:
    """Construct isolated settings without dotenv or ambient profile inheritance."""
    return GryphonConfig(**{"_env_file": None, "ucp_agent_profile": None, "allowed_domains": [], **updates})


def _bad_profiles() -> Iterator[object]:
    """Enumerate URI, interpolation, header, metadata and private-IP rejection cases."""
    yield from (
        None,
        "",
        3,
        "/agent.json",
        "http://operator.example/p",
        "https:///p",
        "https://user@operator.example/p",
    )
    yield from ("https://operator.example/p?", "https://operator.example/p#", "https://operator.example/${PROFILE}")
    yield from (
        "https://operator.example/\r\nHeader:value",
        'https://operator.example/"',
        "https://operator.example/\\p",
    )
    yield from ("https://operator.example/%0D%0A", "https://operator.example/%22", "https://operator.example/\x00")
    yield from ("https://127.0.0.1/p", "https://169.254.169.254/p", "https://168.63.129.16/p", "https://[::1]/p")
    yield from ("https://metadata.google.internal/p", "https://metadata/p", "https://operator.example/" + "a" * 2048)


@pytest.mark.parametrize("value", list(_bad_profiles()))
def test_profile_invalid_values_fail_without_echo(value: object) -> None:
    with pytest.raises(ValueError, match="invalid_profile_url") as caught:
        validate_profile(value, [])
    assert "Header:value" not in str(caught.value)


def test_profile_config_default_and_repr() -> None:
    assert _config().ucp_agent_profile is None
    config = _config(ucp_agent_profile=PROFILE)
    assert config.ucp_agent_profile == PROFILE
    assert PROFILE not in repr(config)


@pytest.mark.parametrize("value", ["", "http://operator.example/p", "https://127.0.0.1/p"])
def test_profile_config_rejects_invalid_setting(value: str) -> None:
    with pytest.raises(ValidationError):
        _config(ucp_agent_profile=value)


def test_profile_config_env_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRYPHON_UCP_AGENT_PROFILE", PROFILE)
    options: dict[str, Any] = {"_env_file": None, "allowed_domains": []}
    config = GryphonConfig(**options)
    assert config.ucp_agent_profile == PROFILE


def test_profile_domain_policy_applies_to_config_and_explicit(endpoint: EndpointManifest) -> None:
    with pytest.raises(ValidationError):
        _config(ucp_agent_profile=PROFILE, allowed_domains=["merchant.example"])
    config = _config(ucp_agent_profile=PROFILE, allowed_domains=["operator.example"])
    arguments = {"json_body": {"id": "cart", "meta": {"ucp-agent": {"profile": CALLER}}}}
    with pytest.raises(ExecutionError, match="UCP agent profile"):
        prepare_arguments(endpoint, arguments, config)
    assert validate_profile(PROFILE, ["operator.example"]) == PROFILE


@pytest.mark.parametrize("change", [{"ucp_agent_profile": ""}, {"allowed_domains": ["merchant.example"]}])
def test_profile_model_copy_is_revalidated(change: dict[str, Any]) -> None:
    config = _config(ucp_agent_profile=PROFILE).model_copy(update=change)
    with pytest.raises(ExecutionError, match="UCP agent profile"):
        configured_profile(config)


@pytest.mark.parametrize("meta", [None, {}, {"ucp-agent": {}}, {"ucp-agent": {"extra": "kept"}, "other": 7}])
def test_profile_omission_filled_without_mutation(endpoint: EndpointManifest, meta: dict[str, Any] | None) -> None:
    arguments: dict[str, Any] = {"json_body": {"id": "cart"}}
    if meta is not None:
        arguments["json_body"]["meta"] = meta
    original = deepcopy(arguments)
    prepared = prepare_arguments(endpoint, arguments, _config(ucp_agent_profile=PROFILE))
    assert prepared["json_body"]["meta"]["ucp-agent"]["profile"] == PROFILE
    assert arguments == original
    assert validate_arguments(endpoint, prepared) == prepared
    if meta and "other" in meta:
        assert prepared["json_body"]["meta"]["other"] == 7
        assert prepared["json_body"]["meta"]["ucp-agent"]["extra"] == "kept"


@pytest.mark.parametrize("profile", [None, "", "http://caller.example/p", 2])
def test_profile_explicit_invalid_never_falls_back(endpoint: EndpointManifest, profile: object) -> None:
    arguments: dict[str, Any] = {"json_body": {"id": "cart", "meta": {"ucp-agent": {"profile": profile}}}}
    with pytest.raises(ExecutionError, match="UCP agent profile"):
        prepare_arguments(endpoint, arguments, _config(ucp_agent_profile=PROFILE))
    assert arguments["json_body"]["meta"]["ucp-agent"]["profile"] == profile


def test_profile_empty_format_annotation_requires_identity_check(endpoint: EndpointManifest) -> None:
    arguments = {"json_body": {"id": "cart", "meta": {"ucp-agent": {"profile": ""}}}}
    assert validate_arguments(endpoint, arguments) == arguments
    with pytest.raises(ExecutionError, match="UCP agent profile"):
        prepare_arguments(endpoint, arguments, _config())


def test_profile_explicit_identity_matches_header_choice(endpoint: EndpointManifest) -> None:
    arguments = {"json_body": {"id": "cart", "meta": {"ucp-agent": {"profile": CALLER}}}}
    config = _config(ucp_agent_profile=PROFILE)
    assert prepare_arguments(endpoint, arguments, config) == arguments
    assert effective_profile(endpoint, arguments["json_body"], config) == CALLER


def test_profile_missing_without_operator_fails_locally(endpoint: EndpointManifest) -> None:
    with pytest.raises(UpstreamDiagnosticError, match="GRYPHON_UCP_AGENT_PROFILE") as caught:
        prepare_arguments(endpoint, {"json_body": {"id": "cart"}}, _config())
    assert caught.value.diagnostic.model_dump(exclude_none=True) == {
        "kind": "upstream",
        "phase": "invoke",
        "upstream_code": "invalid_profile_url",
    }


def test_profile_unwrapped_native_arguments_remain_validation_error(endpoint: EndpointManifest) -> None:
    with pytest.raises(InputValidationError, match="Undeclared"):
        prepare_arguments(endpoint, {"id": "cart"}, _config(ucp_agent_profile=PROFILE))


def test_profile_required_wrapper_can_be_created(endpoint: EndpointManifest) -> None:
    endpoint.input_schema["properties"]["json_body"]["required"] = ["meta"]
    arguments: dict[str, Any] = {}
    prepared = prepare_arguments(endpoint, arguments, _config(ucp_agent_profile=PROFILE))
    assert validate_arguments(endpoint, prepared) == {"json_body": {"meta": {"ucp-agent": {"profile": PROFILE}}}}
    assert arguments == {}


@pytest.mark.parametrize("body", [None, {"meta": None}, {"meta": {"ucp-agent": None}}])
def test_profile_explicit_invalid_containers_are_not_overwritten(endpoint: EndpointManifest, body: Any) -> None:
    arguments = {"json_body": body}
    original = deepcopy(arguments)
    with pytest.raises(UpstreamDiagnosticError):
        prepare_arguments(endpoint, arguments, _config(ucp_agent_profile=PROFILE))
    assert arguments == original


@pytest.mark.parametrize("depth", [0, 1, 2])
def test_profile_only_complete_required_schema_path_is_defaulted(endpoint: EndpointManifest, depth: int) -> None:
    schema = endpoint.input_schema["properties"]["json_body"]
    for key in ("meta", "ucp-agent")[:depth]:
        schema = schema["properties"][key]
    schema["required"] = []
    assert not requires_profile(endpoint)
    arguments = {"json_body": {"id": "cart"}}
    assert prepare_arguments(endpoint, arguments, _config(ucp_agent_profile=PROFILE)) == arguments


def test_profile_required_nullable_leaf_still_checks_identity(endpoint: EndpointManifest) -> None:
    body = endpoint.input_schema["properties"]["json_body"]
    leaf = body["properties"]["meta"]["properties"]["ucp-agent"]["properties"]["profile"]
    leaf["type"] = ["string", "null"]
    assert requires_profile(endpoint)
    with pytest.raises(UpstreamDiagnosticError):
        prepare_arguments(endpoint, {"json_body": {"id": "cart", "meta": {"ucp-agent": {"profile": None}}}}, _config())


def test_profile_ordinary_openapi_never_receives_metadata(endpoint: EndpointManifest) -> None:
    endpoint.mcp_binding = None
    arguments = {"json_body": {"id": "cart"}}
    assert prepare_arguments(endpoint, arguments, _config(ucp_agent_profile=PROFILE)) == arguments


@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], ["93.184.216.34", "169.254.169.254"]])
async def test_profile_every_dns_answer_must_be_public(monkeypatch: pytest.MonkeyPatch, addresses: list[str]) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        assert host == "operator.example" and port == 443
        return addresses

    monkeypatch.setattr("gryphon.security.network.resolve_addresses", resolve)
    with pytest.raises(ExecutionError, match="UCP agent profile"):
        await validate_profile_addresses(PROFILE, _config(allow_private_networks=True))
