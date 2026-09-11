"""Unit tests for the AST security guard."""

from __future__ import annotations

import pytest

from gryphon.errors import SecurityViolationError
from gryphon.security.ast_guard import ASTGuard


@pytest.fixture
def guard() -> ASTGuard:
    return ASTGuard()


# ---------------------------------------------------------------------------
# Safe code — should pass
# ---------------------------------------------------------------------------


def test_plain_math_passes(guard: ASTGuard) -> None:
    guard.validate("result = 2 + 2")


def test_import_httpx_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate("import httpx")


def test_import_json_passes(guard: ASTGuard) -> None:
    guard.validate("import json\nresult = json.dumps({'key': 'value'})")


def test_import_datetime_passes(guard: ASTGuard) -> None:
    guard.validate("from datetime import datetime\nresult = datetime.now().isoformat()")


def test_import_typing_passes(guard: ASTGuard) -> None:
    guard.validate("from typing import Any, Optional\nresult: Any = None")


def test_import_collections_passes(guard: ASTGuard) -> None:
    guard.validate("from collections import defaultdict\nresult = defaultdict(list)")


def test_server_function_import_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate("from weather.functions import get_current_weather\nresult = None")


# ---------------------------------------------------------------------------
# Blocked patterns — must raise SecurityViolationError
# ---------------------------------------------------------------------------


def test_import_os_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate("import os")


def test_import_sys_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate("import sys")


def test_import_subprocess_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate("import subprocess")


def test_import_socket_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate("import socket")


def test_from_os_import_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate("from os import path")


def test_eval_call_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_call"):
        guard.validate("eval('1 + 1')")


def test_exec_call_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_call"):
        guard.validate("exec('x = 1')")


def test_compile_call_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_call"):
        guard.validate("compile('x = 1', '<str>', 'exec')")


def test_dunder_import_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_call"):
        guard.validate("__import__('os')")


def test_open_call_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_call"):
        guard.validate("open('/etc/passwd').read()")


def test_dunder_subclasses_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_attribute"):
        guard.validate("().__class__.__subclasses__()")


def test_dunder_globals_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_attribute"):
        guard.validate("x = {}; x.__globals__")


def test_environ_access_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_attribute"):
        guard.validate("x = something.environ['SECRET']")


def test_invalid_syntax_raises_violation(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="syntax"):
        guard.validate("def broken(: pass")


def test_global_statement_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_global"):
        guard.validate("def f():\n    global x\n    x = 1")


@pytest.mark.parametrize(
    "code",
    [
        "import unknown_module",
        "from unknown_module import value",
        "import _internal",
        "from .json import loads",
        "import json.tool",
        "from json import _default_decoder",
        "from operator import attrgetter",
        "from gzip import open",
        "import pandas",
    ],
)
def test_unknown_or_unsafe_imports_fail_closed(guard: ASTGuard, code: str) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate(code)


@pytest.mark.parametrize("module", ["json", "math", "statistics", "collections.abc", "decimal"])
def test_pure_data_imports_remain_allowed(guard: ASTGuard, module: str) -> None:
    guard.validate(f"import {module}\nresult = 1")


def test_builtin_aliasing_is_rejected(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_call"):
        guard.validate("f = open")


def test_private_module_attributes_are_blocked(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_attribute"):
        guard.validate("import random\nresult = random._os")


def test_capability_call_is_allowed(guard: ASTGuard) -> None:
    guard.validate('result = tool_call("weather", "lookup", {"city": "Paris"})')


def test_syntax_error_does_not_reflect_input(guard: ASTGuard) -> None:
    with pytest.raises(SecurityViolationError, match="^Invalid Python syntax$"):
        guard.validate("'private syntax data")
