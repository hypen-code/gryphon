"""Operator UCP identity validation and narrowly schema-directed native defaults."""

from __future__ import annotations

import ipaddress
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from gryphon.errors import InputValidationError, SecurityViolationError, UpstreamDiagnosticError
from gryphon.models.diagnostics import DiagnosticPhase, ExecutionDiagnostic
from gryphon.security import network
from gryphon.security.policies import check_address_allowed, check_domain_allowed, check_metadata_host, validated_url
from gryphon.security.schema import walk_json

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.models import EndpointManifest

MAX_PROFILE_LENGTH = 2048
_PROFILE_PATH = ("meta", "ucp-agent", "profile")
_PROFILE_ERROR = (
    "invalid_profile_url: supply a public HTTPS meta.ucp-agent.profile or configure "
    "GRYPHON_UCP_AGENT_PROFILE; explicit invalid values are never replaced"
)


def validate_profile(value: object, allowed_domains: list[str]) -> str:
    """Validate a public HTTPS identity URI without fetching or claiming fetchability.

    Args:
        value: Operator configuration or an explicit required native profile.
        allowed_domains: Exact operator hostname policy, also applied to identity URLs.

    Returns:
        The unchanged URI, safe for the fixed quoted UCP-Agent header encoding.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_PROFILE_LENGTH:
        raise ValueError(_PROFILE_ERROR)
    decoded = unquote(value)
    if any(ord(char) <= 32 or ord(char) >= 127 or char in '\\"{}' for char in value + decoded):
        raise ValueError(_PROFILE_ERROR)
    if "?" in value or "#" in value or "$" in value:
        raise ValueError(_PROFILE_ERROR)
    try:
        url = validated_url(value)
        if url.scheme != "https":
            raise ValueError(_PROFILE_ERROR)
        check_domain_allowed(value, allowed_domains)
        check_metadata_host(url.host)
        try:
            address = ipaddress.ip_address(url.host)
        except ValueError:
            address = None
        if address is not None:
            check_address_allowed(str(address))
    except SecurityViolationError:
        raise ValueError(_PROFILE_ERROR) from None
    return value


def _profile_error(phase: DiagnosticPhase = "invoke") -> UpstreamDiagnosticError:
    """Create only fixed allowlisted diagnostic metadata, never echo the identity."""
    return UpstreamDiagnosticError(
        ExecutionDiagnostic(kind="upstream", upstream_code="invalid_profile_url", phase=phase)
    )


def configured_profile(config: GryphonConfig, *, phase: DiagnosticPhase = "invoke") -> str | None:
    """Revalidate operator configuration, including model_copy updates, at use."""
    if config.ucp_agent_profile is None:
        return None
    return _checked_profile(config.ucp_agent_profile, config, phase=phase)


def _checked_profile(value: object, config: GryphonConfig, *, phase: DiagnosticPhase = "invoke") -> str:
    """Translate invalid identities into one static, actionable local failure."""
    try:
        return validate_profile(value, config.allowed_domains)
    except ValueError:
        raise _profile_error(phase) from None


def requires_profile(endpoint: EndpointManifest) -> bool:
    """Recognize only the complete required native metadata path, never OpenAPI."""
    if endpoint.mcp_binding is None:
        return False
    schema = endpoint.input_schema.get("properties", {}).get("json_body", {})
    for key in _PROFILE_PATH:
        if schema.get("type") != "object" or key not in schema.get("required", []):
            return False
        schema = schema.get("properties", {}).get(key, {})
    return True


def effective_profile(endpoint: EndpointManifest, body: Any, config: GryphonConfig) -> str | None:
    """Match the required caller body identity; otherwise use only operator identity."""
    profile = configured_profile(config)
    if not requires_profile(endpoint):
        return profile
    value = body
    for key in _PROFILE_PATH:
        if not isinstance(value, dict) or key not in value:
            raise _profile_error()
        value = value[key]
    return _checked_profile(value, config)


def prepare_arguments(endpoint: EndpointManifest, arguments: dict[str, Any], config: GryphonConfig) -> dict[str, Any]:
    """Copy bounded arguments and fill only omitted, completely required native identity.

    Existing values and sibling fields remain unchanged, including invalid values;
    explicit invalid profiles fail locally instead of falling back to configuration.
    """
    if endpoint.mcp_binding is None:
        return arguments
    profile = configured_profile(config)
    if not requires_profile(endpoint):
        return arguments
    try:
        for _ in walk_json(arguments):
            pass
        result = deepcopy(arguments)
    except (ValueError, TypeError, RecursionError):
        raise InputValidationError("Arguments must be bounded JSON") from None
    if not isinstance(result, dict):
        raise InputValidationError("Arguments must be an object")
    if set(result) - endpoint.input_schema.get("properties", {}).keys():
        raise InputValidationError("Undeclared endpoint arguments are not permitted")
    node = result
    for key in ("json_body", *_PROFILE_PATH[:-1]):
        if key not in node:
            node[key] = {}
        if not isinstance(node[key], dict):
            raise _profile_error()
        node = node[key]
    if "profile" not in node and profile is not None:
        node["profile"] = profile
    effective_profile(endpoint, result["json_body"], config)
    return result


def invocation_metadata(capability: str, endpoint: EndpointManifest, config: GryphonConfig) -> dict[str, Any]:
    """Describe effective defaults without putting an operator identity into generated code."""
    return {
        "usage_example": f'result = await call_tool("{capability}", {{"json_body": inputs}})',
        "ucp_agent_profile": {
            "required": requires_profile(endpoint),
            "operator_configured": configured_profile(config, phase="discovery") is not None,
            "input_path": "json_body.meta.ucp-agent.profile",
            "guidance": "Operator defaults fill omitted required metadata only; explicit invalid values fail.",
        },
    }


async def validate_profile_addresses(profile: str, config: GryphonConfig, *, phase: DiagnosticPhase = "invoke") -> None:
    """Check every DNS answer with existing public address policy, without HTTP fetches."""
    _checked_profile(profile, config, phase=phase)
    url = validated_url(profile)
    try:
        addresses = await network.resolve_addresses(url.host, url.port or 443)
        if not addresses:
            raise SecurityViolationError("Profile hostname resolution failed")
        for address in addresses:
            check_address_allowed(address)
    except SecurityViolationError:
        raise _profile_error(phase) from None
