"""Opt-in real Chromium UI verification with disposable hosted state and synthetic upstream HTTP."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import SecretStr
from test_saas_http import _server
from test_saas_ucp_http import ucp_profile, ucp_schema

from gryphon.config import GryphonConfig
from gryphon.saas_config import SaaSConfig
from gryphon.security.network import NetworkClient


def document(version: int) -> dict[str, object]:
    """Provide a public API with one available GET and one filtered POST operation."""
    response = {"200": {"description": "OK"}}
    return {
        "openapi": "3.0.3",
        "info": {"title": "Browser fixture", "version": str(version)},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/data": {
                "get": {"operationId": f"read_v{version}", "responses": response},
                "post": {"operationId": "create", "responses": response},
            }
        },
    }


def network_factory() -> tuple[object, list[str]]:
    """Return a pinned mock transport factory; all destinations stay offline."""
    seen: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        """Advance a synthetic URL document once, then return it unchanged."""
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        seen.append(request.url.path)
        if request.url.path == "/.well-known/ucp":
            return httpx.Response(200, json=ucp_profile(expanded=seen.count("/.well-known/ucp") > 1))
        if request.url.path == "/schemas/shopping.openapi.json":
            return httpx.Response(200, json=ucp_schema())
        assert request.url.path == "/openapi.json"
        return httpx.Response(200, json=document(min(seen.count("/openapi.json"), 2)))

    def factory(config: GryphonConfig) -> NetworkClient:
        """Retain production DNS-pinning behavior with a deterministic public resolver."""
        return NetworkClient(
            config, resolver=AsyncMock(return_value=["93.184.216.34"]), transport=httpx.MockTransport(transport)
        )

    return factory, seen


async def browser(origin: str, token: str, root: Path) -> int:
    """Pass generated credentials privately over stdin, never output or persist them."""
    process = await asyncio.create_subprocess_exec(
        "node", str(Path(__file__).with_name("browser_ui.cjs")), stdin=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(120):
            await process.communicate(json.dumps({"origin": origin, "token": token, "root": str(root)}).encode())
        return process.returncode if process.returncode is not None else 1
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def main(root: Path) -> int:
    """Start the real hosted app, mock upstream requests, and release all temporary resources."""
    for version in range(1, 4):
        (root / f"spec-{version}.json").write_text(json.dumps(document(version)))
    token = secrets.token_urlsafe(48)
    options: dict[str, Any] = {"_env_file": None}
    config = SaaSConfig(
        **options,
        admin_token=SecretStr(token),
        database_url=SecretStr("sqlite:///:memory:"),
        public_origin="http://127.0.0.1",
        allow_insecure_http=True,
        state_dir=root / "hosted",
    )
    base = GryphonConfig(
        **options,
        cache_db_path=str(root / "cache.db"),
        run_db_path=str(root / "runs.db"),
        artifact_dir=str(root / "artifacts"),
    )
    factory, seen = network_factory()
    with patch("gryphon.saas_spec_import.NetworkClient", factory), patch("gryphon.compiler.ucp.NetworkClient", factory):
        async with _server(config, base) as (origin, _):
            result = await browser(origin, token, root)
    if result == 0:
        assert seen[:3] == ["/openapi.json"] * 3
        assert seen[3:] == ["/.well-known/ucp", "/schemas/shopping.openapi.json"] * 2
    return result


if __name__ == "__main__":
    with (
        tempfile.TemporaryDirectory(prefix="gryphon-browser-ui-") as directory,
        patch.dict(
            os.environ, {key: value for key, value in os.environ.items() if not key.startswith("GRYPHON_")}, clear=True
        ),
    ):
        raise SystemExit(asyncio.run(main(Path(directory))))
