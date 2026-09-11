"""Unit tests for gryphon/security/policies.py and gryphon/security/vault.py."""

from __future__ import annotations

import pytest

from gryphon.errors import SecurityViolationError
from gryphon.models import JwtAuthConfig, StaticAuthConfig
from gryphon.security.policies import check_domain_allowed, enforce_read_only
from gryphon.security.vault import (
    build_all_server_env_vars,
    build_server_env_vars,
    resolve_auth_config,
    resolve_auth_env_vars,
    resolve_env_references,
)

# ---------------------------------------------------------------------------
# enforce_read_only
# ---------------------------------------------------------------------------


def test_enforce_read_only_get_is_allowed() -> None:
    """GET is not mutating — should not raise."""
    enforce_read_only("GET", "weather")  # must not raise


def test_enforce_read_only_post_raises() -> None:
    with pytest.raises(SecurityViolationError, match="read-only"):
        enforce_read_only("POST", "weather")


def test_enforce_read_only_put_raises() -> None:
    with pytest.raises(SecurityViolationError):
        enforce_read_only("PUT", "hotel")


def test_enforce_read_only_patch_raises() -> None:
    with pytest.raises(SecurityViolationError):
        enforce_read_only("PATCH", "hotel")


def test_enforce_read_only_delete_raises() -> None:
    with pytest.raises(SecurityViolationError):
        enforce_read_only("DELETE", "hotel")


def test_enforce_read_only_lowercase_post_raises() -> None:
    """Method string is case-normalised before checking."""
    with pytest.raises(SecurityViolationError):
        enforce_read_only("post", "weather")


def test_enforce_read_only_head_is_allowed() -> None:
    enforce_read_only("HEAD", "weather")  # must not raise


# ---------------------------------------------------------------------------
# check_domain_allowed
# ---------------------------------------------------------------------------


def test_check_domain_allowed_empty_list_permits_all() -> None:
    """An empty allowlist adds no hostname restriction, but URL validation remains."""
    check_domain_allowed("https://anything.example.com/v1", [])  # must not raise


def test_check_domain_allowed_matching_exact_domain() -> None:
    check_domain_allowed("https://api.weather.com/v1", ["api.weather.com"])  # must not raise


def test_check_domain_allowed_subdomain_rejected() -> None:
    """Subdomains require their own explicit allowlist entry."""
    with pytest.raises(SecurityViolationError):
        check_domain_allowed("https://sub.example.com/v1", ["example.com"])  # must raise


def test_check_domain_allowed_blocked_domain_raises() -> None:
    with pytest.raises(SecurityViolationError, match="not in the allowed domains"):
        check_domain_allowed("https://evil.example.com/v1", ["safe.com"])


def test_check_domain_allowed_multiple_domains_one_matches() -> None:
    check_domain_allowed("https://api.example.com/v1", ["other.com", "api.example.com"])  # must not raise


def test_check_domain_allowed_multiple_domains_none_match_raises() -> None:
    with pytest.raises(SecurityViolationError):
        check_domain_allowed("https://bad.io/v1", ["safe.com", "good.org"])


# ---------------------------------------------------------------------------
# resolve_env_references
# ---------------------------------------------------------------------------


def test_resolve_env_references_no_placeholders() -> None:
    assert resolve_env_references("Bearer hardcoded-key") == "Bearer hardcoded-key"


def test_resolve_env_references_resolves_existing_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SECRET_TOKEN", "supersecret")
    result = resolve_env_references("Bearer ${MY_SECRET_TOKEN}")
    assert result == "Bearer supersecret"


def test_resolve_env_references_unresolvable_left_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_VAR", raising=False)
    result = resolve_env_references("Bearer ${MISSING_VAR}")
    # Unresolvable placeholders stay unchanged
    assert "${MISSING_VAR}" in result


def test_resolve_env_references_multiple_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST", "api.example.com")
    monkeypatch.setenv("PORT", "8080")
    result = resolve_env_references("http://${HOST}:${PORT}/v1")
    assert result == "http://api.example.com:8080/v1"


def test_resolve_env_references_empty_string() -> None:
    assert resolve_env_references("") == ""


# ---------------------------------------------------------------------------
# build_server_env_vars
# ---------------------------------------------------------------------------


def test_build_server_env_vars_no_env_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRYPHON_WEATHER_BASE_URL", raising=False)
    monkeypatch.delenv("GRYPHON_WEATHER_AUTH", raising=False)
    result = build_server_env_vars("weather")
    assert result == {}


def test_build_server_env_vars_base_url_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRYPHON_WEATHER_BASE_URL", "https://api.weather.example.com/v1")
    monkeypatch.delenv("GRYPHON_WEATHER_AUTH", raising=False)
    result = build_server_env_vars("weather")
    assert result["GRYPHON_WEATHER_BASE_URL"] == "https://api.weather.example.com/v1"
    assert "GRYPHON_WEATHER_AUTH" not in result


def test_build_server_env_vars_auth_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRYPHON_HOTEL_AUTH", "Bearer test-token")
    monkeypatch.delenv("GRYPHON_HOTEL_BASE_URL", raising=False)
    result = build_server_env_vars("hotel")
    assert result["GRYPHON_HOTEL_AUTH"] == "Bearer test-token"


def test_build_server_env_vars_auth_resolves_references(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_API_KEY", "resolved-key")
    monkeypatch.setenv("GRYPHON_MYAPI_AUTH", "Bearer ${MY_API_KEY}")
    result = build_server_env_vars("myapi")
    assert result["GRYPHON_MYAPI_AUTH"] == "Bearer resolved-key"


def test_build_server_env_vars_uppercase_server_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server name is upper-cased when building env var keys."""
    monkeypatch.setenv("GRYPHON_PETSTORE_BASE_URL", "https://petstore.example.com")
    result = build_server_env_vars("petstore")
    assert "GRYPHON_PETSTORE_BASE_URL" in result


# ---------------------------------------------------------------------------
# build_all_server_env_vars
# ---------------------------------------------------------------------------


def test_build_all_server_env_vars_empty_list() -> None:
    result = build_all_server_env_vars([])
    assert result == {}


def test_build_all_server_env_vars_combines_multiple(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRYPHON_SVC1_BASE_URL", "https://svc1.example.com")
    monkeypatch.setenv("GRYPHON_SVC2_BASE_URL", "https://svc2.example.com")
    result = build_all_server_env_vars(["svc1", "svc2"])
    assert "GRYPHON_SVC1_BASE_URL" in result
    assert "GRYPHON_SVC2_BASE_URL" in result


def test_build_all_server_env_vars_single_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRYPHON_ONLY_BASE_URL", "https://only.example.com")
    result = build_all_server_env_vars(["only"])
    assert result["GRYPHON_ONLY_BASE_URL"] == "https://only.example.com"


# ---------------------------------------------------------------------------
# resolve_auth_config — static / jwt
# ---------------------------------------------------------------------------


def test_resolve_auth_config_static_passthrough() -> None:
    auth = StaticAuthConfig(value="Bearer hardcoded")
    assert resolve_auth_config("svc", auth) == "Bearer hardcoded"


def test_resolve_auth_config_static_resolves_env_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "resolved")
    auth = StaticAuthConfig(value="Bearer ${MY_TOKEN}")
    assert resolve_auth_config("svc", auth) == "Bearer resolved"


def test_resolve_auth_config_jwt_prefixes_bearer() -> None:
    auth = JwtAuthConfig(token="a.b.c")
    assert resolve_auth_config("svc", auth) == "Bearer a.b.c"


def test_resolve_auth_config_jwt_resolves_env_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_JWT", "x.y.z")
    auth = JwtAuthConfig(token="${MY_JWT}")
    assert resolve_auth_config("svc", auth) == "Bearer x.y.z"


# ---------------------------------------------------------------------------
# build_server_env_vars with auth_config
# ---------------------------------------------------------------------------


def test_build_server_env_vars_with_static_auth_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRYPHON_SVC_BASE_URL", raising=False)
    auth = StaticAuthConfig(value="Bearer direct")
    result = build_server_env_vars("svc", auth_config=auth)
    assert result["GRYPHON_SVC_AUTH"] == "Bearer direct"


def test_build_server_env_vars_auth_config_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRYPHON_SVC_AUTH", "Bearer from-env")
    auth = StaticAuthConfig(value="Bearer from-config")
    result = build_server_env_vars("svc", auth_config=auth)
    assert result["GRYPHON_SVC_AUTH"] == "Bearer from-config"


# ---------------------------------------------------------------------------
# resolve_auth_env_vars
# ---------------------------------------------------------------------------


def test_resolve_auth_env_vars_static_sets_auth_key() -> None:
    auth = StaticAuthConfig(value="Bearer xyz")
    result = resolve_auth_env_vars("mysvc", auth)
    assert result == {"GRYPHON_MYSVC_AUTH": "Bearer xyz"}


def test_resolve_auth_env_vars_jwt_sets_auth_key() -> None:
    auth = JwtAuthConfig(token="a.b.c")
    result = resolve_auth_env_vars("mysvc", auth)
    assert result == {"GRYPHON_MYSVC_AUTH": "Bearer a.b.c"}


# ---------------------------------------------------------------------------
# build_server_env_vars — cookie env var (legacy path)
# ---------------------------------------------------------------------------


def test_build_server_env_vars_reads_cookie_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy credential helpers remain available to trusted host callers only."""
    monkeypatch.setenv("GRYPHON_LEGACY_COOKIE", "JSESSIONID=legacyval")
    monkeypatch.delenv("GRYPHON_LEGACY_BASE_URL", raising=False)
    result = build_server_env_vars("legacy")
    assert result["GRYPHON_LEGACY_COOKIE"] == "JSESSIONID=legacyval"
