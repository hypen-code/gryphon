"""Bounded JSON shape metadata without artifact values or partial key names."""

from __future__ import annotations

import json
from typing import Any

_MAX_KEYS = 32
_MAX_KEY_BYTES = 512
_JSON_TYPES = {
    dict: "object",
    list: "array",
    str: "string",
    int: "number",
    float: "number",
    bool: "boolean",
    type(None): "null",
}


def artifact_shape(value: Any, *, key_budget: int = _MAX_KEY_BYTES) -> dict[str, Any]:
    """Describe top-level JSON structure without disclosing value previews.

    Args:
        value: Already validated JSON-native artifact data.
        key_budget: Optional smaller key-list byte budget; never exceeds the hard ceiling.

    Returns:
        JSON type; objects add complete keys, total key count and explicit
        truncation, arrays add length. Key JSON is bounded to 512 bytes/32 keys.
        Oversized names are omitted, never shortened into ambiguous names.
    """
    shape: dict[str, Any] = {"json_type": _JSON_TYPES.get(type(value), "unknown")}
    if type(value) is dict:
        key_budget = min(_MAX_KEY_BYTES, max(2, key_budget))
        keys: list[str] = []
        size = 2
        for key in value:
            cost = len(json.dumps(key, ensure_ascii=True).encode("utf-8")) + bool(keys)
            if len(keys) >= _MAX_KEYS or size + cost > key_budget:
                break
            keys.append(key)
            size += cost
        shape.update(top_level_keys=keys, key_count=len(value), keys_truncated=len(keys) != len(value))
    elif type(value) is list:
        shape["length"] = len(value)
    return shape


def projection_hint(artifact_id: str) -> dict[str, str]:
    """Prefer a local projection over re-running the upstream recipe for oversized data."""
    return {
        "tool": "transform_artifact",
        "artifact_id": artifact_id,
        "guidance": "Project inputs['artifact']; optional parameters are inputs['params']. No upstream calls.",
    }
