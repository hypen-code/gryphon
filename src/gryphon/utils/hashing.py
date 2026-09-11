"""Hashing utilities for swagger version detection and cache keys."""

from __future__ import annotations

import hashlib


def hash_content(content: str | bytes) -> str:
    """Compute SHA256 hash of string or bytes content.

    Args:
        content: Content to hash — string or bytes.

    Returns:
        Lowercase hexadecimal SHA256 digest (64 chars).
    """
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def hash_code(code: str) -> str:
    """Hash Python code string for cache key generation.

    Args:
        code: Python source code to hash.

    Returns:
        SHA256 hex digest, normalizing only Python source line endings.
        Blank lines and trailing spaces are significant inside string literals.
    """
    # Normalize whitespace to avoid trivial cache misses
    normalized = code.replace("\r\n", "\n").replace("\r", "\n")
    return hash_content(normalized)


def combine_hashes(*hashes: str) -> str:
    """Combine multiple hashes into a single hash.

    Args:
        *hashes: Individual hash strings to combine.

    Returns:
        SHA256 hash of sorted, joined input hashes.
    """
    combined = "|".join(sorted(hashes))
    return hash_content(combined)
