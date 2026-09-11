"""Unit tests for AuthConfig discriminated union and SwaggerSource auth field."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from gryphon.models import (
    BasicAuthConfig,
    ExecutionScope,
    JwtAuthConfig,
    KeycloakAuthConfig,
    OAuth2AuthConfig,
    SessionAuthConfig,
    StaticAuthConfig,
    SwaggerSource,
)

_BASE_SOURCE: dict[str, Any] = {
    "name": "svc",
    "swagger_url": "/fake/path.yaml",
    "base_url": "https://api.example.com",
}


# ---------------------------------------------------------------------------
# StaticAuthConfig
# ---------------------------------------------------------------------------


def test_static_auth_config_parses() -> None:
    auth = StaticAuthConfig(value="Bearer tok123")
    assert auth.type == "static"
    assert auth.value == "Bearer tok123"


def test_static_auth_config_via_swagger_source() -> None:
    src = SwaggerSource(**_BASE_SOURCE, auth={"type": "static", "value": "Bearer x"})  # type: ignore[arg-type]
    assert isinstance(src.auth, StaticAuthConfig)
    assert src.auth.value == "Bearer x"


# ---------------------------------------------------------------------------
# JwtAuthConfig
# ---------------------------------------------------------------------------


def test_jwt_auth_config_parses() -> None:
    auth = JwtAuthConfig(token="a.b.c")
    assert auth.type == "jwt"
    assert auth.token == "a.b.c"


def test_jwt_auth_config_via_swagger_source() -> None:
    src = SwaggerSource(**_BASE_SOURCE, auth={"type": "jwt", "token": "x.y.z"})  # type: ignore[arg-type]
    assert isinstance(src.auth, JwtAuthConfig)
    assert src.auth.token == "x.y.z"


# ---------------------------------------------------------------------------
# OAuth2AuthConfig
# ---------------------------------------------------------------------------


def test_oauth2_auth_config_parses() -> None:
    auth = OAuth2AuthConfig(
        token_url="https://auth.example.com/token",
        client_id="cid",
        client_secret="csecret",
        scope="api:read",
    )
    assert auth.type == "oauth2"
    assert auth.token_url == "https://auth.example.com/token"
    assert auth.scope == "api:read"


def test_oauth2_auth_config_scope_optional() -> None:
    auth = OAuth2AuthConfig(
        token_url="https://auth.example.com/token",
        client_id="cid",
        client_secret="csecret",
    )
    assert auth.scope == ""


def test_oauth2_auth_config_via_swagger_source() -> None:
    src = SwaggerSource(
        **_BASE_SOURCE,
        auth={  # type: ignore[arg-type]
            "type": "oauth2",
            "token_url": "https://auth.example.com/token",
            "client_id": "cid",
            "client_secret": "sec",
        },
    )
    assert isinstance(src.auth, OAuth2AuthConfig)
    assert src.auth.client_id == "cid"


# ---------------------------------------------------------------------------
# KeycloakAuthConfig
# ---------------------------------------------------------------------------


def test_keycloak_auth_config_parses() -> None:
    auth = KeycloakAuthConfig(
        base_url="https://keycloak.example.com/auth",
        realm="myrealm",
        client_id="gryphon",
        client_secret="secret",
        scope="openid",
    )
    assert auth.type == "keycloak"
    assert auth.realm == "myrealm"


def test_keycloak_auth_config_via_swagger_source() -> None:
    src = SwaggerSource(
        **_BASE_SOURCE,
        auth={  # type: ignore[arg-type]
            "type": "keycloak",
            "base_url": "https://keycloak.example.com/auth",
            "realm": "myrealm",
            "client_id": "gryphon",
            "client_secret": "sec",
        },
    )
    assert isinstance(src.auth, KeycloakAuthConfig)
    assert src.auth.realm == "myrealm"


# ---------------------------------------------------------------------------
# Backward compat: auth_header promoted to StaticAuthConfig
# ---------------------------------------------------------------------------


def test_auth_header_compat_promoted_to_static() -> None:
    src = SwaggerSource(**_BASE_SOURCE, auth_header="Bearer legacy-token")
    assert isinstance(src.auth, StaticAuthConfig)
    assert src.auth.value == "Bearer legacy-token"


def test_auth_header_basic_promoted_to_static() -> None:
    src = SwaggerSource(**_BASE_SOURCE, auth_header="Basic dXNlcjpwYXNz")
    assert isinstance(src.auth, StaticAuthConfig)
    assert src.auth.value == "Basic dXNlcjpwYXNz"


def test_auth_field_takes_precedence_over_auth_header() -> None:
    """Explicit auth: block wins; auth_header is ignored when auth is set."""
    src = SwaggerSource(
        **_BASE_SOURCE,
        auth_header="Bearer old-token",
        auth={"type": "static", "value": "Bearer new-token"},  # type: ignore[arg-type]
    )
    assert isinstance(src.auth, StaticAuthConfig)
    assert src.auth.value == "Bearer new-token"


def test_no_auth_gives_none() -> None:
    src = SwaggerSource(**_BASE_SOURCE)
    assert src.auth is None


# ---------------------------------------------------------------------------
# Invalid auth type
# ---------------------------------------------------------------------------


def test_unknown_auth_type_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        SwaggerSource(**_BASE_SOURCE, auth={"type": "kerberos", "ticket": "xxx"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SessionAuthConfig
# ---------------------------------------------------------------------------


def test_session_auth_config_parses_defaults() -> None:
    auth = SessionAuthConfig(
        login_url="https://app.example.com/login",
        username="admin",
        password="secret",
    )
    assert auth.type == "session"
    assert auth.username_field == "username"
    assert auth.password_field == "password"
    assert auth.content_type == "json"
    assert auth.cookie_name == ""
    assert auth.token_field == ""
    assert auth.expires_seconds == 3600


def test_session_auth_config_custom_fields() -> None:
    auth = SessionAuthConfig(
        login_url="https://app.example.com/login",
        username="admin",
        password="${APP_PASSWORD}",
        username_field="user",
        password_field="pass",
        content_type="form",
        cookie_name="JSESSIONID",
        expires_seconds=1800,
    )
    assert auth.username_field == "user"
    assert auth.password_field == "pass"
    assert auth.content_type == "form"
    assert auth.cookie_name == "JSESSIONID"
    assert auth.expires_seconds == 1800


def test_session_auth_config_token_field() -> None:
    auth = SessionAuthConfig(
        login_url="https://app.example.com/api/login",
        username="${API_USER}",
        password="${API_PASS}",
        token_field="access_token",
    )
    assert auth.token_field == "access_token"


def test_session_auth_config_via_swagger_source() -> None:
    src = SwaggerSource(
        **_BASE_SOURCE,
        auth={  # type: ignore[arg-type]
            "type": "session",
            "login_url": "https://app.example.com/login",
            "username": "admin",
            "password": "${APP_PASS}",
            "cookie_name": "JSESSIONID",
        },
    )
    assert isinstance(src.auth, SessionAuthConfig)
    assert src.auth.cookie_name == "JSESSIONID"


def test_session_auth_invalid_content_type_raises() -> None:
    with pytest.raises(ValidationError):
        SessionAuthConfig(
            login_url="https://app.example.com/login",
            username="u",
            password="p",
            content_type="xml",  # type: ignore[arg-type]
        )


def test_basic_auth_discriminated_union() -> None:
    """Basic credentials retain their supported configuration entry point."""
    source = SwaggerSource.model_validate(
        {
            **_BASE_SOURCE,
            "auth": {"type": "basic", "username": "test-user", "password": "test-password"},
        }
    )
    assert isinstance(source.auth, BasicAuthConfig)


def test_execution_scope_defaults_are_host_owned() -> None:
    """The broker accepts the shared execution authority contract."""
    scope = ExecutionScope(run_id="run-test", deadline=1234.0)
    assert (scope.owner, scope.max_calls, scope.calls, scope.cancelled) == ("local", 50, 0, False)
