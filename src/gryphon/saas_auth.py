"""Bounded in-memory administrator sessions; channel keys are independently stored."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gryphon.saas_config import SaaSConfig

COOKIE_NAME = "gryphon_admin"


class RateLimiter:
    """Bound requests with a fixed monotonic window and constant memory."""

    def __init__(self, limit: int, seconds: int = 60) -> None:
        """Initialize a server-owned window; never use unverified forwarded identities."""
        self.limit, self.seconds = limit, seconds
        self.start, self.count = time.monotonic(), 0

    def accept(self) -> bool:
        """Consume one request or reject until the next window."""
        now = time.monotonic()
        if now - self.start >= self.seconds:
            self.start, self.count = now, 0
        if self.count >= self.limit:
            return False
        self.count += 1
        return True


class AdminSessions:
    """Keep only session-cookie digests, CSRF tokens and monotonic expirations."""

    def __init__(self, config: SaaSConfig) -> None:
        """Snapshot bounded policy and hash the independent administrator credential."""
        self._admin_digest = self._digest(config.admin_token.get_secret_value())
        self._ttl, self._limit = config.session_ttl_seconds, config.max_admin_sessions
        self._sessions: dict[str, tuple[str, float]] = {}

    @staticmethod
    def _digest(value: str) -> str:
        """Hash high-entropy tokens without retaining their plaintext."""
        return hashlib.sha256(value.encode()).hexdigest()

    def _expire(self) -> None:
        """Discard expired sessions before admission or authentication."""
        now = time.monotonic()
        self._sessions = {key: value for key, value in self._sessions.items() if value[1] > now}

    def login(self, token: str) -> tuple[str, str] | None:
        """Issue an opaque fresh session only after constant-time credential verification."""
        self._expire()
        if not hmac.compare_digest(self._admin_digest, self._digest(token)) or len(self._sessions) >= self._limit:
            return None
        cookie, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        self._sessions[self._digest(cookie)] = csrf, time.monotonic() + self._ttl
        return cookie, csrf

    def verify(self, cookie: str) -> str | None:
        """Return the session CSRF token, never derive identity from browser claims."""
        self._expire()
        record = self._sessions.get(self._digest(cookie))
        return record[0] if record else None

    def logout(self, cookie: str) -> None:
        """Revoke a session idempotently without affecting other administrators."""
        self._sessions.pop(self._digest(cookie), None)

    def close(self) -> None:
        """Invalidate all sessions when the hosted process stops."""
        self._sessions.clear()
