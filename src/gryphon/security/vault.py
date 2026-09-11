"""Host-only credential compatibility helpers; never pass these values to sandboxes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from gryphon.errors import ConfigurationError, ExecutionError, SecurityViolationError
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from gryphon.models import AuthConfig, SessionAuthConfig

logger = get_logger(__name__)

# Pattern to resolve ${VAR_NAME} references in config values
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

# Deprecated OAuth2 cache compatibility symbol; credentials are only cached by AsyncVault
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

# Deprecated session cache compatibility symbol; never populated or read by the broker
_SESSION_CACHE: dict[str, tuple[AuthResult, float]] = {}

_CACHE_SKEW_SECONDS = 30  # refresh this many seconds before actual expiry


@dataclass(repr=False)
class AuthResult:
    """Resolved credentials for a server — carries either an Authorization value,
    a Cookie value, or both (when session login returns a token AND sets cookies).
    """

    auth_header: str = field(default="")  # value for the Authorization: header
    cookie: str = field(default="")  # value for the Cookie: header


def resolve_env_references(value: str) -> str:
    """Resolve ${VAR} environment variable references in a string.

    Args:
        value: String potentially containing ${VAR_NAME} references.

    Returns:
        String with all resolvable references replaced by env values.
        Unresolvable references are left as-is and a warning is logged.
    """

    def replace_ref(match: re.Match[str]) -> str:
        var_name = match.group(1)
        resolved = os.environ.get(var_name)
        if resolved is None:
            logger.warning("env_var_not_found")
            return match.group(0)  # Leave placeholder unchanged
        return resolved

    return _ENV_VAR_PATTERN.sub(replace_ref, value)


def resolve_auth_config(server_name: str, auth: AuthConfig) -> str:
    """Resolve a non-session AuthConfig to a ready-to-use Authorization header value.

    For static/jwt types the value is returned immediately.
    OAuth2/Keycloak use a verified one-shot pool; async callers use AsyncVault.
    Only AsyncVault maintains a broker-local credential cache.

    NOTE: For session auth use resolve_auth_env_vars() which returns the full
    dict including Cookie headers.

    Args:
        server_name: Used as cache key and for log context.
        auth: Parsed AuthConfig (not SessionAuthConfig).

    Returns:
        Full Authorization header value, e.g. "Bearer eyJ...".
    """
    import base64

    from gryphon.models import BasicAuthConfig, JwtAuthConfig, StaticAuthConfig

    if isinstance(auth, StaticAuthConfig):
        return resolve_env_references(auth.value)

    if isinstance(auth, JwtAuthConfig):
        return f"Bearer {resolve_env_references(auth.token)}"

    if isinstance(auth, BasicAuthConfig):
        username = resolve_env_references(auth.username)
        password = resolve_env_references(auth.password)
        if "${" in username or "${" in password:
            raise ConfigurationError("Authentication configuration could not be resolved")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return f"Basic {token}"

    # OAuth2 / Keycloak — compatibility calls do not share global credential caches
    bearer, _ = _fetch_oauth2_token(server_name, auth)
    return bearer


def resolve_auth_env_vars(server_name: str, auth: AuthConfig) -> dict[str, str]:
    """Resolve an AuthConfig to the GRYPHON_{SERVER}_* env vars it produces.

    For most auth types this sets GRYPHON_{SERVER}_AUTH (Authorization header).
    For session auth it may set GRYPHON_{SERVER}_COOKIE instead, or both when
    the login endpoint returns a token in the response body AND sets cookies.

    Args:
        server_name: Server name — used for env var prefix, cache key, and logging.
        auth: Parsed AuthConfig from SwaggerSource.

    Returns:
        Legacy-named ``GRYPHON_{SERVER}_AUTH``/``COOKIE`` values for trusted host
        callers only. Never inject this mapping into a sandbox.
    """
    from gryphon.models import SessionAuthConfig

    prefix = f"GRYPHON_{server_name.upper()}_"

    if isinstance(auth, SessionAuthConfig):
        result = _resolve_session_auth(server_name, auth)
        env: dict[str, str] = {}
        if result.auth_header:
            env[f"{prefix}AUTH"] = result.auth_header
        if result.cookie:
            env[f"{prefix}COOKIE"] = result.cookie
        return env

    return {f"{prefix}AUTH": resolve_auth_config(server_name, auth)}


def _fetch_oauth2_token(server_name: str, auth: AuthConfig) -> tuple[str, float]:
    """Fetch an OAuth2 client credentials token via the shared verified boundary.

    Args:
        server_name: Only used for error messages.
        auth: OAuth2AuthConfig or KeycloakAuthConfig.

    Returns:
        (bearer_header_value, expiry_epoch_seconds) tuple.

    Raises:
        RuntimeError: When the token endpoint returns an error.
    """
    from gryphon.models import KeycloakAuthConfig, OAuth2AuthConfig

    if not isinstance(auth, KeycloakAuthConfig | OAuth2AuthConfig):  # Narrow the supported dynamic auth union
        raise ConfigurationError("Use resolve_auth_env_vars for session authentication")
    try:
        headers = _resolve_dynamic_sync(server_name, auth)  # All token requests use the shared network policy
    except (ExecutionError, SecurityViolationError):
        raise RuntimeError("OAuth2 token fetch failed") from None  # Never include upstream diagnostic data
    return headers.get("Authorization", ""), time.time()  # One-shot credentials are not globally cached


def _resolve_session_auth(server_name: str, auth: SessionAuthConfig) -> AuthResult:
    """Log in synchronously through the shared verified network boundary.

    Flow:
      1. POST credentials to auth.login_url (JSON or form-encoded).
      2a. If auth.token_field is set → extract that field from the JSON response
          body and return it as an Authorization: Bearer token.
      2b. Otherwise → collect cookies from the response.
          If auth.cookie_name is set, only that cookie is extracted.
          If empty, all cookies are joined as "name=value; name2=value2".
      3. Return host-only credentials; use AsyncVault for broker-local TTL caching.

    Args:
        server_name: Used as cache key and for error messages.
        auth: SessionAuthConfig with login details.

    Returns:
        AuthResult with auth_header and/or cookie populated.

    Raises:
        RuntimeError: On login failure or missing expected token/cookie.
    """
    try:
        headers = _resolve_dynamic_sync(server_name, auth)
    except (ExecutionError, SecurityViolationError) as exc:
        raise RuntimeError(f"Session login failed: {exc}") from None
    # Extract bearer token from the broker-local validated login result
    auth_header = headers.get("Authorization", "")
    # Extract cookies from the broker-local validated login result
    return AuthResult(auth_header=auth_header, cookie=headers.get("Cookie", ""))


def build_server_env_vars(
    server_name: str,
    auth_config: AuthConfig | None = None,
) -> dict[str, str]:
    """Build a legacy-named credential mapping for trusted host callers only.

    When auth_config is provided (from SwaggerSource.auth), it takes precedence
    and tokens are fetched/cached as needed. Falls back to GRYPHON_{SERVER}_AUTH and
    GRYPHON_{SERVER}_COOKIE env vars for servers whose auth was baked in at compile time.

    Credentials are NEVER embedded in generated code.

    Args:
        server_name: Name of the server (e.g., "weather").
        auth_config: Optional typed auth config from SwaggerSource.auth.

    Returns:
        Host-only credential mapping. Sandbox injection is prohibited.
    """
    prefix = f"GRYPHON_{server_name.upper()}_"
    env_vars: dict[str, str] = {}

    base_url_key = f"{prefix}BASE_URL"
    auth_key = f"{prefix}AUTH"
    cookie_key = f"{prefix}COOKIE"
    extra_headers_key = f"{prefix}EXTRA_HEADERS"

    base_url = os.environ.get(base_url_key, "")
    extra_headers = os.environ.get(extra_headers_key, "")

    if base_url:
        env_vars[base_url_key] = base_url

    if auth_config is not None:
        env_vars.update(resolve_auth_env_vars(server_name, auth_config))
    else:
        # Legacy path: credentials were baked into process env at compile/serve time
        auth = os.environ.get(auth_key, "")
        if auth:
            env_vars[auth_key] = resolve_env_references(auth)
        cookie = os.environ.get(cookie_key, "")
        if cookie:
            env_vars[cookie_key] = cookie  # cookies are not ${VAR}-expanded

    if extra_headers:
        env_vars[extra_headers_key] = extra_headers

    return env_vars


def build_all_server_env_vars(
    server_names: list[str],
    auth_configs: dict[str, AuthConfig | None] | None = None,
) -> dict[str, str]:
    """Build combined env vars for all required servers.

    Args:
        server_names: List of server names to build credentials for.
        auth_configs: Optional mapping of server name to AuthConfig.

    Returns:
        Combined dict of all server environment variables.
    """
    combined: dict[str, str] = {}
    for name in server_names:
        cfg = (auth_configs or {}).get(name)
        combined.update(build_server_env_vars(name, cfg))
    return combined


def resolve_broker_env_headers(server_name: str, *, include_credentials: bool = True) -> dict[str, str]:
    """Read legacy credentials only in the trusted host broker.

    Args:
        server_name: Canonical registry identifier.
        include_credentials: Whether to include legacy auth/cookie values.

    Returns:
        HTTP credentials and administrator extra headers; BASE_URL is ignored.
    """
    from gryphon.security.encoding import validate_header

    env = build_server_env_vars(server_name)
    prefix = f"GRYPHON_{server_name.upper()}_"
    headers: dict[str, str] = {}
    raw = env.get(prefix + "EXTRA_HEADERS", "")
    if raw:
        try:
            extras = json.loads(raw)
        except ValueError:
            raise ConfigurationError("Invalid administrator extra headers") from None
        if not isinstance(extras, dict):
            raise ConfigurationError("Invalid administrator extra headers")
        for name, value in extras.items():
            if not isinstance(value, str):
                raise ConfigurationError("Invalid administrator extra headers")
            validate_header(name, value, trusted=True)
            headers[name] = value
    for suffix, name in (("AUTH", "Authorization"), ("COOKIE", "Cookie")):
        if include_credentials and env.get(prefix + suffix):
            headers[name] = env[prefix + suffix]
    return headers


def _resolve_dynamic_sync(server_name: str, auth: AuthConfig) -> dict[str, str]:
    """Keep synchronous host callers safe; async callers must use AsyncVault."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_resolve_dynamic_once(server_name, auth))
    raise ConfigurationError("Dynamic authentication in async code requires AsyncVault.resolve")


async def _resolve_dynamic_once(server_name: str, auth: AuthConfig) -> dict[str, str]:
    """Resolve using the same verified network boundary with a short-lived pool."""
    from gryphon.config import GryphonConfig
    from gryphon.security.auth import AsyncVault
    from gryphon.security.network import NetworkClient

    network = NetworkClient(GryphonConfig())
    vault = AsyncVault(network)
    try:
        return await vault.resolve(server_name, auth)
    finally:
        vault.clear()
        await network.close()
