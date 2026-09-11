"""Gryphon offline compute entrypoint, running only INSIDE a hardened container.

Read one bounded JSON request from stdin and emit one strict JSON envelope.
The host prepares explicit final-expression/result/return semantics; main is
never automatically called. No credentials or compiled host files are present.
CPython builtin filtering is defence in depth, NOT a security boundary: the
networkless container and explicitly configured runtime provide that boundary.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import io
import json
import math
import os
import signal
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

_MAX_REQUEST_BYTES = 18 * 1024 * 1024
_MAX_SOURCE_BYTES = 1024 * 1024
_MAX_JSON_NODES = 100_000
_MAX_JSON_DEPTH = 32


class _OutputCounter(io.TextIOBase):
    """Discard untrusted stdout/stderr while enforcing a shared producer budget."""

    def __init__(self, limit: int) -> None:
        """Initialize a bounded counter without allocating output buffers."""
        self.limit = limit
        self.count = 0
        self.exceeded = False

    def write(self, text: str) -> int:
        """Count UTF-8 bytes and reject overflow even if user code catches it."""
        self.count += len(text.encode("utf-8"))
        if self.count > self.limit:
            self.exceeded = True
            raise BufferError("Output limit exceeded")
        return len(text)

    def flush(self) -> None:
        """Implement the standard stream interface without retaining content."""


def _install_timeout(seconds: int) -> None:
    """Install an uncatchable hard process timeout independent of user exceptions."""

    def stop(signum: int, frame: object) -> None:
        """Exit immediately; Docker host cleanup always removes this container."""
        os._exit(124)

    signal.signal(signal.SIGALRM, stop)
    signal.alarm(seconds)


def _read_request() -> dict[str, Any]:
    """Read a bounded line without legacy code-bearing environment variables."""
    raw = sys.stdin.buffer.readline(_MAX_REQUEST_BYTES + 1)
    if len(raw) > _MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        raise ValueError("Invalid request frame")
    request = json.loads(raw)
    if not isinstance(request, dict) or not isinstance(request.get("code"), str):
        raise ValueError("Invalid request")
    if len(request["code"].encode()) > _MAX_SOURCE_BYTES or not isinstance(request.get("inputs"), dict):
        raise ValueError("Invalid code or inputs")
    for name, maximum in (("timeout", 300), ("max_output", 1048576), ("max_response", 16777216)):
        if type(request.get(name)) is not int or not 1 <= request[name] <= maximum:
            raise ValueError("Invalid execution limit")
    return request


async def _execute(code: str, inputs: dict[str, Any]) -> Any:
    """Evaluate prepared Python exactly once, including top-level await."""
    # 3. Block dangerous builtins
    # Security is defence-in-depth alongside the AST guard and Docker limits.
    # __import__ is kept so offline compute imports work normally.
    blocked = {"open", "exec", "eval", "compile", "input", "breakpoint"}
    safe_builtins = {key: value for key, value in vars(builtins).items() if key not in blocked}
    # Single namespace for globals and locals so top-level imports are visible
    # inside functions defined in the same code block.
    namespace: dict[str, Any] = {"__builtins__": safe_builtins, "inputs": inputs}
    tree = ast.parse(code, filename="<gryphon>")
    if not tree.body or not isinstance(tree.body[-1], ast.Expr):
        raise ValueError("Expected a prepared final result expression")
    final = tree.body[-1]
    tree.body[-1] = ast.Assign(targets=[ast.Name(id="_gryphon_output", ctx=ast.Store())], value=final.value)
    ast.fix_missing_locations(tree)
    compiled = compile(tree, "<gryphon>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    coroutine = eval(compiled, namespace)  # noqa: S307
    if coroutine is not None:
        await coroutine
    return namespace["_gryphon_output"]


def _encode_result(data: Any, limit: int) -> str:
    """Reject non-JSON output instead of silently coercing objects to strings."""
    pending = [(data, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        kind = type(value)
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise BufferError("JSON structure exceeds limit")
        if kind is dict:
            if any(type(key) is not str for key in value):
                raise ValueError("JSON keys must be strings")
            pending.extend((child, depth + 1) for child in value.values())
        elif kind is list:
            pending.extend((child, depth + 1) for child in value)
        elif kind is str:
            if len(value.encode()) > limit:
                raise BufferError("JSON value exceeds limit")
        elif kind not in (int, float, bool, type(None)):
            raise ValueError("Result is not JSON-native")
        elif kind is float and not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        if len(pending) > _MAX_JSON_NODES:
            raise BufferError("JSON structure exceeds limit")
    chunks: list[str] = []
    size = 0
    for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":")).iterencode(data):
        size += len(chunk.encode())
        if size > limit:
            raise BufferError("JSON output exceeds limit")
        chunks.append(chunk)
    return "".join(chunks)


def main() -> None:
    """Receive, execute once, and emit bounded JSON without raw errors or traces."""
    real_stdout = sys.stdout
    try:
        # 1. Read code
        request = _read_request()
        # 2. Install hard timeout (SIGALRM — Linux only, not available on Windows)
        _install_timeout(request["timeout"])
        # 4. Redirect stdout so user print() calls don't corrupt the JSON line
        captured = _OutputCounter(request["max_output"])
        with redirect_stdout(captured), redirect_stderr(captured):
            result = asyncio.run(_execute(request["code"], request["inputs"]))
            if captured.exceeded:
                raise BufferError("Output limit exceeded")
            output = _encode_result(
                {"success": True, "data": result, "printed_bytes": captured.count}, request["max_response"]
            )
    except (MemoryError, RecursionError, BufferError):
        output = '{"success":false,"error_type":"capacity"}'
    except BaseException:
        output = '{"success":false,"error_type":"execution"}'
    finally:
        # Cancel the alarm — execution completed in time
        signal.alarm(0)
    real_stdout.write(output + "\n")
    real_stdout.flush()


if __name__ == "__main__":
    main()
