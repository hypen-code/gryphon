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


def test_user_sessions_cannot_exhaust_platform_recovery_capacity() -> None:
    """Bound one account's sessions and reserve platform access under tenant pressure."""
    config = settings(max_admin_sessions=6)
    sessions = AdminSessions(config)
    issued = [sessions.login_user("tenant-user", 1) for _ in range(4)]
    assert all(issued)
    assert sessions.login_user("tenant-user", 1) is None
    assert sessions.login_user("other-user", 1) is not None
    assert sessions.login_user("third-user", 1) is None
    admin = sessions.login(config.admin_token.get_secret_value())
    assert admin is not None
    sessions.logout(admin[0])
    assert sessions.login_user("platform-user", 1, platform=True) is not None
    sessions.close()
    assert all(sessions.identity(item[0]) is None for item in issued if item is not None)


def test_user_session_expiry_and_logout_release_account_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired or revoked cookies cannot retain named identity or block future login."""
    now = [100.0]
    monkeypatch.setattr("gryphon.saas_auth.time.monotonic", lambda: now[0])
    config = settings(max_admin_sessions=1)
    sessions = AdminSessions(config)
    first = sessions.login_user("user", 3)
    assert first is not None and sessions.identity(first[0]) == ("user", 3)
    assert sessions.login_user("user", 3) is None
    sessions.logout(first[0])
    assert sessions.identity(first[0]) is None
    second = sessions.login_user("user", 4)
    assert second is not None
    now[0] += config.session_ttl_seconds + 1
    assert sessions.verify(second[0]) is None and sessions.identity(second[0]) is None
    assert sessions.login_user("user", 4) is not None


def test_user_session_revocation_preserves_new_revisions_and_other_accounts() -> None:
    """Committed account changes proactively discard only older affected sessions."""
    sessions = AdminSessions(settings())
    old = sessions.login_user("one", 1)
    new = sessions.login_user("one", 2)
    other = sessions.login_user("two", 1)
    assert old is not None and new is not None and other is not None
    sessions.revoke_user("one", before_revision=2)
    assert sessions.verify(old[0]) is None and sessions.identity(old[0]) is None
    assert sessions.verify(new[0]) == new[1]
    assert sessions.verify(other[0]) == other[1]


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
