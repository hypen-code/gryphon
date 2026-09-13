"""Versioned stdlib password hashing with bounded, cancellation-safe worker admission."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets

from gryphon.errors import CapacityError, SaaSValidationError
from gryphon.runtime.execution_cleanup import finish_cleanup

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128
PASSWORD_MAX_BYTES = 512
HASH_ITERATIONS = 600000
HASH_BYTES = 32
HASH_PARALLELISM = 2
HASH_ADMISSION = 8
HASH_PREFIX = "pbkdf2-sha256$v1$600000$"
HASH_LENGTH = len(HASH_PREFIX) + HASH_BYTES * 4 + 1
HASH_PATTERN = re.compile(r"pbkdf2-sha256\$v1\$600000\$([0-9a-f]{64})\$([0-9a-f]{64})", re.ASCII)
DUMMY_SALT = bytes(HASH_BYTES)
DUMMY_DIGEST = bytes(HASH_BYTES)


def password_bytes(password: str) -> bytes:
    """Validate character and UTF-8 byte bounds before scheduling expensive work."""
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise SaaSValidationError("Password length is invalid")
    try:
        encoded = password.encode("utf-8")
    except UnicodeError:
        raise SaaSValidationError("Password encoding is invalid") from None
    if len(encoded) > PASSWORD_MAX_BYTES:
        raise SaaSValidationError("Password byte limit exceeded")
    return encoded


class PasswordHasher:
    """Share two workers and eight total admissions across one control database."""

    def __init__(self) -> None:
        """Allocate instance-local worker permits without starting background work."""
        self._slots = asyncio.Semaphore(HASH_PARALLELISM)
        self._admitted = 0

    async def _derive(self, password: bytes, salt: bytes) -> bytes:
        """Reject saturation and retain admission until the real worker has finished."""
        if self._admitted >= HASH_ADMISSION:
            raise CapacityError("Password hashing capacity exceeded")
        self._admitted += 1
        try:
            async with self._slots:
                return await finish_cleanup(
                    asyncio.to_thread(hashlib.pbkdf2_hmac, "sha256", password, salt, HASH_ITERATIONS, HASH_BYTES)
                )
        finally:
            self._admitted -= 1

    async def hash_password(self, password: str) -> str:
        """Return one fixed-format hash with a fresh cryptographic 32-byte salt."""
        encoded = password_bytes(password)
        salt = secrets.token_bytes(HASH_BYTES)
        digest = await self._derive(encoded, salt)
        return f"{HASH_PREFIX}{salt.hex()}${digest.hex()}"

    async def verify_password(self, password: str, stored: str | None) -> bool:
        """Perform one fixed-cost derivation even for absent or malformed credentials."""
        try:
            encoded = password_bytes(password)
            valid_password = True
        except SaaSValidationError:
            encoded, valid_password = b"invalid-password", False
        match = HASH_PATTERN.fullmatch(stored) if stored is not None and len(stored) == HASH_LENGTH else None
        salt = bytes.fromhex(match[1]) if match else DUMMY_SALT
        expected = bytes.fromhex(match[2]) if match else DUMMY_DIGEST
        actual = await self._derive(encoded, salt)
        equal = hmac.compare_digest(actual, expected)
        return equal and match is not None and valid_password
