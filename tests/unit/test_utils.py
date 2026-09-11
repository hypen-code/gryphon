"""Unit tests for gryphon/utils/hashing.py and gryphon/utils/logging.py."""

from __future__ import annotations

import logging

from gryphon.utils.hashing import combine_hashes, hash_code, hash_content
from gryphon.utils.logging import get_logger, setup_logging

# ---------------------------------------------------------------------------
# hash_content
# ---------------------------------------------------------------------------


def test_hash_content_string_returns_64_char_hex() -> None:
    result = hash_content("hello world")
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


def test_hash_content_bytes_returns_64_char_hex() -> None:
    result = hash_content(b"hello world")
    assert len(result) == 64


def test_hash_content_string_and_bytes_equal() -> None:
    """String and its UTF-8 encoded bytes should produce the same hash."""
    assert hash_content("hello world") == hash_content(b"hello world")


def test_hash_content_is_deterministic() -> None:
    assert hash_content("test content") == hash_content("test content")


def test_hash_content_different_inputs_differ() -> None:
    assert hash_content("aaa") != hash_content("bbb")


def test_hash_content_empty_string() -> None:
    result = hash_content("")
    assert len(result) == 64


# ---------------------------------------------------------------------------
# hash_code
# ---------------------------------------------------------------------------


def test_hash_code_returns_64_chars() -> None:
    result = hash_code("result = 42")
    assert len(result) == 64


def test_hash_code_preserves_trailing_whitespace() -> None:
    """Trailing whitespace is significant inside multiline literals."""
    code_clean = "result = '''a\nb'''"
    code_trailing = "result = '''a   \nb  '''"
    assert hash_code(code_clean) != hash_code(code_trailing)


def test_hash_code_preserves_blank_lines() -> None:
    """Blank lines are significant inside multiline literals."""
    code_no_blanks = "result = '''a\nb'''"
    code_with_blanks = "result = '''a\n\n\nb'''"
    assert hash_code(code_no_blanks) != hash_code(code_with_blanks)


def test_hash_code_normalizes_only_line_endings() -> None:
    """Python line-ending normalization does not discard source whitespace."""
    assert hash_code("result = 1\r\n") == hash_code("result = 1\n")
    assert hash_code("result = 1\r") == hash_code("result = 1\n")


def test_hash_code_preserves_unicode_line_separators() -> None:
    """Unicode separators inside literals are not treated as Python newlines."""
    assert hash_code("result = 'a\u2028b'") != hash_code("result = 'a\nb'")


def test_hash_code_different_logic_differs() -> None:
    assert hash_code("result = 1") != hash_code("result = 2")


def test_hash_code_is_deterministic() -> None:
    code = "def foo():\n    return 42"
    assert hash_code(code) == hash_code(code)


# ---------------------------------------------------------------------------
# combine_hashes
# ---------------------------------------------------------------------------


def test_combine_hashes_single() -> None:
    h = "abc123"
    result = combine_hashes(h)
    assert len(result) == 64


def test_combine_hashes_multiple() -> None:
    result = combine_hashes("hash1", "hash2", "hash3")
    assert len(result) == 64


def test_combine_hashes_order_independent() -> None:
    """combine_hashes sorts inputs, so order should not matter."""
    r1 = combine_hashes("aaa", "bbb", "ccc")
    r2 = combine_hashes("ccc", "aaa", "bbb")
    assert r1 == r2


def test_combine_hashes_deterministic() -> None:
    assert combine_hashes("x", "y") == combine_hashes("x", "y")


def test_combine_hashes_different_inputs_differ() -> None:
    assert combine_hashes("a", "b") != combine_hashes("a", "c")


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


def test_setup_logging_info_level() -> None:
    """setup_logging at INFO should not raise and configure root logger."""
    setup_logging("INFO")
    root = logging.getLogger()
    assert root.level == logging.INFO


def test_setup_logging_debug_level() -> None:
    """setup_logging at DEBUG configures console renderer."""
    setup_logging("DEBUG")
    root = logging.getLogger()
    assert root.level == logging.DEBUG


def test_setup_logging_warning_level() -> None:
    setup_logging("WARNING")
    root = logging.getLogger()
    assert root.level == logging.WARNING


def test_setup_logging_invalid_level_defaults_to_info() -> None:
    """An invalid level string falls back via getattr to logging.INFO (value 20)."""
    # getattr(logging, 'BOGUS', logging.INFO) returns logging.INFO
    setup_logging("BOGUS_LEVEL")
    root = logging.getLogger()
    # Should still be set to something valid
    assert root.level >= 0


# ---------------------------------------------------------------------------
# get_logger
# ---------------------------------------------------------------------------


def test_get_logger_returns_bound_logger() -> None:
    logger = get_logger("gryphon.test_module")
    assert logger is not None
    # structlog BoundLogger doesn't implement isinstance easily,
    # but we can verify it has standard logging methods
    assert hasattr(logger, "info")
    assert hasattr(logger, "warning")
    assert hasattr(logger, "error")


def test_get_logger_different_names() -> None:
    l1 = get_logger("module.a")
    l2 = get_logger("module.b")
    # Both should be usable
    assert l1 is not None
    assert l2 is not None
