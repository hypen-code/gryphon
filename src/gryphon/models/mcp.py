"""Trusted fixed MCP transport bindings, separate from untrusted API documents."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gryphon.errors import SecurityViolationError
from gryphon.security.policies import validated_url


class MCPBinding(BaseModel):
    """Bind one catalog alias to an exact HTTPS endpoint and discovered tool contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str = Field(min_length=1, max_length=2048)
    tool_name: str = Field(min_length=1, max_length=128)
    tool_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        """Reject credential-bearing or ambiguous transport authority without URL disclosure."""
        try:
            url = validated_url(value)
            if (
                url.scheme != "https"
                or not url.host
                or url.userinfo
                or url.query
                or url.fragment
                or any(char.isspace() for char in value)
                or any(char in value for char in "{}\\")
                or "${" in value
            ):
                raise ValueError("Invalid MCP endpoint")
        except (ValueError, SecurityViolationError):
            raise ValueError("MCP endpoint requires absolute HTTPS without credentials, query or fragment") from None
        return str(url)

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, value: str) -> str:
        """Bound native tool names independently of normalized Python aliases."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("Invalid MCP tool name")
        return value
