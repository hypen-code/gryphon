"""Single-worker hosted application combining administration and isolated channel MCP endpoints."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from gryphon.saas_api import AdminAPI
from gryphon.saas_gateway import MCPGateway
from gryphon.saas_http import HTTPBoundary
from gryphon.saas_runtime import ChannelRuntimeManager
from gryphon.saas_store import SaaSStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request

    from gryphon.config import GryphonConfig
    from gryphon.saas_config import SaaSConfig

_ASSETS = Path(__file__).parent


def create_app(config: SaaSConfig, base: GryphonConfig) -> Starlette:
    """Compose explicit hosted dependencies; initialization happens only inside lifespan."""
    store = SaaSStore(config.database_url.get_secret_value(), max_tenants=100, max_spec_bytes=config.max_spec_bytes)
    runtimes = ChannelRuntimeManager(base, config.state_dir, config.max_runtimes)
    admin = AdminAPI(config, base, store, runtimes)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        """Acquire exclusive database ownership before serving; close partial startup state."""
        async with AsyncExitStack() as stack:
            stack.push_async_callback(store.close)
            await store.initialize()
            await store.acquire_host_lease()
            stack.push_async_callback(runtimes.close)
            stack.callback(admin.sessions.close)
            yield

    async def index(request: Request) -> FileResponse:
        """Serve packaged static markup with no embedded configuration or credentials."""
        return FileResponse(_ASSETS / "templates" / "admin.html", media_type="text/html")

    async def health(request: Request) -> JSONResponse:
        """Confirm database readiness without exposing tenant metadata or operator settings."""
        await store.list_tenants(limit=1)
        return JSONResponse({"status": "ready"})

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/health", health),
            *admin.routes(),
            Mount("/static", StaticFiles(directory=_ASSETS / "static")),
            Mount("/mcp", MCPGateway(store, runtimes)),
        ],
        lifespan=lifespan,
        middleware=[Middleware(HTTPBoundary, config=config)],
    )
    app.state.store, app.state.runtimes, app.state.admin = store, runtimes, admin
    return app


async def serve(config: SaaSConfig, base: GryphonConfig) -> int:
    """Run one bounded hosted worker without access logs, proxy-header trust, or ambient env files."""
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(config, base),
            host=config.host,
            port=config.port,
            workers=1,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            log_level="warning",
            limit_concurrency=config.max_http_requests + 8,
            timeout_keep_alive=5,
            timeout_graceful_shutdown=base.execution_timeout_seconds + 10,
        )
    )
    await server.serve()
    return 0 if server.started else 1
