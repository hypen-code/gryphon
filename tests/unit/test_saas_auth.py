"""Hosted administration session and configuration security contracts."""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from gryphon.saas_auth import AdminSessions, RateLimiter
from gryphon.saas_config import SaaSConfig


def settings(**overrides: Any) -> SaaSConfig:
    """Build isolated settings without reading operator configuration."""
    return SaaSConfig(
        admin_token=SecretStr(secrets.token_urlsafe(48)),
        database_url=SecretStr("sqlite:///:memory:"),
        public_origin="https://gryphon.example.com",
        **overrides,
    )


def test_sessions_login_rotates_and_requires_csrf() -> None:
    """Only a verified administrator gets an expiring session and CSRF token."""
    config = settings()
    sessions = AdminSessions(config)
    assert sessions.login("invalid") is None
    session = sessions.login(config.admin_token.get_secret_value())
    assert session is not None
    cookie, csrf = session
    assert sessions.verify(cookie) == csrf
    assert sessions.verify("unknown") is None
    sessions.logout(cookie)
    assert sessions.verify(cookie) is None


def test_sessions_expiry_and_capacity_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired sessions are removed and live-session capacity fails closed."""
    clock = [100.0]
    monkeypatch.setattr("gryphon.saas_auth.time.monotonic", lambda: clock[0])
    config = settings(max_admin_sessions=1)
    sessions = AdminSessions(config)
    first = sessions.login(config.admin_token.get_secret_value())
    assert first is not None
    assert sessions.login(config.admin_token.get_secret_value()) is None
    clock[0] += config.session_ttl_seconds + 1
    assert sessions.verify(first[0]) is None
    assert sessions.login(config.admin_token.get_secret_value()) is not None


def test_rate_limiter_rejects_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed global window bounds unauthenticated attempts without an IP map."""
    clock = [10.0]
    monkeypatch.setattr("gryphon.saas_auth.time.monotonic", lambda: clock[0])
    limit = RateLimiter(2, 60)
    assert limit.accept()
    assert limit.accept()
    assert not limit.accept()
    clock[0] += 61
    assert limit.accept()


@pytest.mark.parametrize("origin", ["http://example.com", "https://user:pass@example.com", "https://a.test/path"])
def test_settings_unsafe_public_origin_rejected(origin: str) -> None:
    """Hosted origin must be HTTPS and contain neither credentials nor paths."""
    with pytest.raises(ValidationError):
        SaaSConfig(
            admin_token=SecretStr(secrets.token_urlsafe(48)),
            database_url=SecretStr("sqlite:///:memory:"),
            public_origin=origin,
        )


def test_settings_insecure_http_requires_loopback() -> None:
    """Explicit development opt-in never permits insecure public origins."""
    config = SaaSConfig(
        admin_token=SecretStr(secrets.token_urlsafe(48)),
        database_url=SecretStr("sqlite:///:memory:"),
        public_origin="http://127.0.0.1:8000",
        allow_insecure_http=True,
    )
    assert not config.secure_cookies
    with pytest.raises(ValidationError):
        SaaSConfig(
            admin_token=SecretStr(secrets.token_urlsafe(48)),
            database_url=SecretStr("sqlite:///:memory:"),
            public_origin="http://example.com",
            allow_insecure_http=True,
        )
