"""Exact operator attestations for read-only POST routes, never document-derived authority."""

from __future__ import annotations

import keyword
import re
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, field_validator


class ReadOnlyPostOperation(BaseModel):
    """Attest one literal POST route on one server and effective base URL as read-only.

    Permits are deployment operator settings, not uploaded specification fields.
    Names, descriptions and extension hints cannot create or broaden a permit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    server_name: str
    base_url: str
    path: str
    method: Literal["POST"] = "POST"

    @field_validator("server_name")
    @classmethod
    def _server(cls, value: str) -> str:
        """Require the canonical catalog namespace, without wildcard matching."""
        if not re.fullmatch(r"[a-z][a-z0-9_]*", value) or keyword.iskeyword(value):
            raise ValueError("Read-only POST permits require an exact canonical server name")
        return value

    @field_validator("base_url")
    @classmethod
    def _base(cls, value: str) -> str:
        """Reject ambiguous or credential-bearing destinations without consulting DNS."""
        try:
            url = httpx.URL(value)
        except httpx.InvalidURL:
            raise ValueError("Invalid read-only POST base URL") from None
        if (
            url.scheme not in {"https", "http"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or any(char.isspace() or char in "{}\\*%?#" for char in value)
            or any(part in {".", ".."} for part in value.split("/"))
        ):
            raise ValueError("Read-only POST permits require an exact HTTP(S) base URL")
        return value.rstrip("/")

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        """Limit attestations to literal routes; templates and wildcards are not grants."""
        if (
            not value.startswith("/")
            or value.startswith("//")
            or any(char.isspace() or char in "{}\\*%?#" for char in value)
            or any(part in {".", ".."} for part in value.split("/"))
        ):
            raise ValueError("Read-only POST permits require an exact literal route")
        return value

    def matches(self, server_name: str, method: str, base_url: str, path: str) -> bool:
        """Match all routing authority exactly, without interpreting operation labels."""
        return (self.server_name, self.method, self.base_url, self.path) == (server_name, method, base_url, path)
