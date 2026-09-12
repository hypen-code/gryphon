"""Server-owned discovery modes with byte-bounded, reachable function continuations."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from gryphon.errors import InputValidationError
from gryphon.runtime.context import MAX_CONTEXT_BYTES, bounded_page, json_bytes, validate_page

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.models import ServerInfo
    from gryphon.runtime.registry import Registry


def list_servers_description(config: GryphonConfig) -> str:
    """Describe the active server-owned mode without exposing a client mode switch."""
    mode = (
        "Discover servers with function names and descriptions. All summaries are included when they fit. "
        "Follow next_cursor and next_function_cursor together as cursor and function_cursor; "
        "a nonzero function cursor continues the same server. Truncated descriptions are marked. "
        if config.include_function_summaries
        else "Discover a compact page of servers, without dumping all function names. "
        "Follow next_cursor as cursor; function_cursor must remain zero. "
    )
    return mode + (
        "cursor defaults to 0; limit defaults to 10 (1–100 servers, capped by discovery_limit). "
        "Results include the registry fingerprint; restart pagination if it changes. "
        "Use search_functions to search and get_functions for full schemas."
    )


def list_servers_page(
    registry: Registry, config: GryphonConfig, cursor: int, limit: int, function_cursor: int
) -> dict[str, Any]:
    """Return compact metadata by default or explicitly paginated function descriptions."""
    validate_page(cursor, limit)
    validate_page(function_cursor, limit)
    servers = sorted(registry.list_servers(), key=lambda server: server.name)
    _validate_cursor(servers, cursor, function_cursor, config.include_function_summaries)
    budget = min(config.context_budget_bytes, MAX_CONTEXT_BYTES)
    limit = min(limit, config.discovery_limit)
    if not config.include_function_summaries:
        items = [_server_metadata(server) for server in servers[cursor : cursor + limit]]
        return bounded_page(
            "servers", items, registry.fingerprint(), budget, cursor=cursor, total=len(servers), limit=limit
        )
    return _summary_page(registry, servers, cursor, limit, function_cursor, budget)


def _validate_cursor(servers: list[ServerInfo], cursor: int, function_cursor: int, summaries: bool) -> None:
    """Reject invalid positions, including function positions not tied to a live server."""
    if cursor > len(servers) or (function_cursor and not summaries):
        raise InputValidationError("Invalid discovery cursor")
    if function_cursor and (cursor == len(servers) or function_cursor >= len(servers[cursor].functions)):
        raise InputValidationError("Invalid function cursor")


def _server_metadata(server: ServerInfo) -> dict[str, Any]:
    """Preserve the existing compact server shape."""
    return {"name": server.name, "description": server.description, "function_count": len(server.functions)}


def _summary_page(
    registry: Registry, servers: list[ServerInfo], cursor: int, limit: int, function_cursor: int, budget: int
) -> dict[str, Any]:
    """Consume ordered functions without treating the server limit as a function limit."""
    result: dict[str, Any] = {
        "servers": [],
        "registry_fingerprint": registry.fingerprint(),
        "total": len(servers),
        "truncated": False,
        "next_cursor": None,
        "next_function_cursor": None,
    }
    for index in range(cursor, min(cursor + limit, len(servers))):
        server = servers[index]
        names = sorted(server.functions)
        start = function_cursor if index == cursor else 0
        for position in range(start, len(names)) if names else [0]:
            function = None
            if names:
                info = registry.get_function(server.name, names[position])
                function = {"name": names[position], "description": info.description or info.summary}
            next_server = index if position + 1 < len(names) else index + 1
            next_function = position + 1 if next_server == index else 0
            candidate = _candidate(result, server, function, next_server, next_function, len(servers))
            if len(json_bytes(candidate)) > budget:
                if result["servers"]:
                    return result
                candidate = _clip_first(candidate, budget)
            result = candidate
    return result


def _candidate(
    result: dict[str, Any],
    server: ServerInfo,
    function: dict[str, str] | None,
    next_server: int,
    next_function: int,
    total: int,
) -> dict[str, Any]:
    """Reserve coherent continuation bytes before accepting each complete function summary."""
    rows = list(result["servers"])
    row = {**rows.pop()} if rows and rows[-1]["name"] == server.name else {**_server_metadata(server), "functions": []}
    row["functions"] = [*row["functions"], function] if function is not None else []
    rows.append(row)
    more = next_server < total
    clipped = any(item.get("truncated") or any(fn.get("truncated") for fn in item["functions"]) for item in rows)
    return {
        **result,
        "servers": rows,
        "truncated": more or clipped,
        "next_cursor": next_server if more else None,
        "next_function_cursor": next_function if more else None,
    }


def _clip_first(candidate: dict[str, Any], budget: int) -> dict[str, Any]:
    """Clip only an otherwise unreturnable first entry, keeping arrays and forward progress."""
    result = deepcopy(candidate)
    result["truncated"] = True
    row = result["servers"][0]
    fields = [row, *row["functions"]]
    originals = [item["description"] for item in fields]
    for item in fields:
        item["truncated"] = True
    _fit_text(result, fields, originals, "description", budget)
    if len(json_bytes(result)) > budget:
        for item in fields:
            item["name_truncated"] = True
        _fit_text(result, fields, [item["name"] for item in fields], "name", budget)
    return result


def _fit_text(
    result: dict[str, Any], fields: list[dict[str, Any]], originals: list[str], field: str, budget: int
) -> None:
    """Find a deterministic common character ceiling using exact UTF-8 JSON measurements."""
    low, high = 0, max(map(len, originals), default=0)
    while low < high:
        middle = (low + high + 1) // 2
        for item, text in zip(fields, originals, strict=True):
            item[field] = text[:middle]
        if len(json_bytes(result)) <= budget:
            low = middle
        else:
            high = middle - 1
    for item, text in zip(fields, originals, strict=True):
        item[field] = text[:low]
