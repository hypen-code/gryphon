"""Gryphon CLI: compile, serve, run, reversible clean, and read-only doctor."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import os
import signal
import sys
import time
from contextlib import AsyncExitStack, suppress
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv
from pydantic import SecretStr

from gryphon import __version__
from gryphon.config import _ENV_FILE, GryphonConfig, load_config
from gryphon.errors import CacheError
from gryphon.utils.logging import get_logger, setup_logging

_PR_SET_PDEATHSIG = 1

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from gryphon.models import AuthConfig
    from gryphon.runtime.registry import Registry
    from gryphon.security.broker import ToolBroker
    from gryphon.server import ActivityClock


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(prog="gryphon", description="Gryphon: bounded, brokered Python execution")
    parser.add_argument("--version", action="version", version=f"Gryphon {__version__}")
    parser.add_argument("--env-file", default=None, metavar="PATH", help="Use a custom .env file")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # clean subcommand
    clean_parser = subparsers.add_parser("clean", help="Archive generated output and cache; stop the server first")
    clean_parser.add_argument("then", nargs="?", choices=["compile"], metavar="compile")
    clean_parser.add_argument("--yes", action="store_true", help="Confirm archival after stopping the server")
    _compile_options(clean_parser)

    # compile subcommand
    compile_parser = subparsers.add_parser("compile", help="Compile OpenAPI sources and print MCP client JSON")
    _compile_options(compile_parser)

    # serve subcommand
    serve_parser = subparsers.add_parser("serve", help="Start MCP; compile first unless compile_on_startup is false")
    _transport_options(serve_parser)

    # run subcommand (compile + serve)
    run_parser = subparsers.add_parser("run", help="Compile once, then start the MCP server")
    _transport_options(run_parser)
    doctor_parser = subparsers.add_parser("doctor", help="Read-only JSON diagnostics; never starts services")
    stdio_parser = subparsers.add_parser("stdio", help="Compile environment sources and serve using private user state")
    saas_parser = subparsers.add_parser("saas", help="Start hosted administration and tenant/channel HTTP endpoints")
    for subparser in (clean_parser, compile_parser, serve_parser, run_parser, doctor_parser, stdio_parser, saas_parser):
        subparser.add_argument("--env-file", default=argparse.SUPPRESS, metavar="PATH", help="Use a custom .env file")
    return parser


def _compile_options(parser: argparse.ArgumentParser) -> None:
    """Keep compile options consistent across compile and clean compile."""
    parser.add_argument("--llm-enhance", action="store_true", help="Request optional LLM enhancement if supported")
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing output or archiving")


def _transport_options(parser: argparse.ArgumentParser) -> None:
    """Share explicit transport overrides between serve and run."""
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default=None, help="HTTP bind address (default: configured loopback)")
    parser.add_argument("--port", type=int, default=None, help="HTTP port")


def _config_for(args: argparse.Namespace) -> GryphonConfig:
    """Reuse one validated settings instance across composed commands."""
    from gryphon.cli_setup import load_stdio_config

    config = getattr(args, "_config", None)
    if not isinstance(config, GryphonConfig):
        loader = load_stdio_config if getattr(args, "command", None) == "stdio" else load_config
        if getattr(args, "command", None) == "saas":
            config = load_config(getattr(args, "env_file", None), discover_env=False)
        else:
            config = loader(getattr(args, "env_file", None))
        args._config = config
    return config


async def _cmd_clean(args: argparse.Namespace) -> int:
    """Archive recognized generated files only, with explicit confirmation."""
    from gryphon.cli_clean import archive_outputs

    config = _config_for(args)
    if not getattr(args, "yes", False):
        get_logger(__name__).error("clean_confirmation_required", action="Stop the server first, then use clean --yes")
        return 1
    archive_outputs(config, dry_run=getattr(args, "dry_run", False))
    if getattr(args, "then", None) == "compile":
        return await _cmd_compile(args)
    return 0


async def _cmd_compile(args: argparse.Namespace) -> int:
    """Compile sources and print non-secret client JSON only for standalone compilation."""
    from gryphon.compiler.orchestrator import Orchestrator  # noqa: PLC0415

    config = _config_for(args)
    if getattr(args, "llm_enhance", False):
        config.llm_enhance = True
    orchestrator = Orchestrator(config, env_file=getattr(args, "env_file", None) or str(_ENV_FILE))
    dry_run = getattr(args, "dry_run", False)
    result = await orchestrator.compile_all(dry_run=dry_run)
    get_logger(__name__).info(
        "compile_summary",
        compiled=len(result.compiled),
        skipped=len(result.skipped),
        failed=len(result.failed),
        total_endpoints=result.total_endpoints,
    )
    if args.command in {"compile", "clean"} and not dry_run and not result.failed and result.mcp_json:
        sys.stdout.write(result.mcp_json + "\n")
    return 1 if result.failed else 0


def _prepare_transport(args: argparse.Namespace, config: GryphonConfig) -> bool:
    """Validate overrides and require strong HTTP authentication before any startup."""
    logger = get_logger(__name__)
    if getattr(args, "host", None) is not None:
        config.host = args.host
    port = getattr(args, "port", None)
    if port is not None:
        if not 1 <= port <= 65535:
            logger.error("invalid_http_port", action="Choose a port between 1 and 65535")
            return False
        config.port = port
    if getattr(args, "transport", "stdio") == "http":
        token = config.http_auth_token
        if not isinstance(token, SecretStr) or len(token.get_secret_value()) < 32:
            logger.error("http_auth_required", action="Set GRYPHON_HTTP_AUTH_TOKEN to at least 32 characters")
            return False
    return True


def _die_with_parent() -> None:
    """Best-effort Linux parent-death signal so an abandoned stdio server cannot linger.

    An MCP host that dies without closing the child's stdin would otherwise leave a
    process holding the exclusive run-ledger lease, blocking every later launch.
    """
    if sys.platform != "linux":
        return
    try:
        ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except (OSError, AttributeError):
        return


async def _cmd_serve(config_args: argparse.Namespace, *, compiled: bool = False) -> int:
    """Compile when configured, then own every partially started dependency."""
    from gryphon.errors import DockerUnavailableError  # noqa: PLC0415

    config = _config_for(config_args)
    if not _prepare_transport(config_args, config):
        return 1
    if config.compile_on_startup and not compiled and await _cmd_compile(config_args):
        return 1
    try:
        return await _serve_started(config_args, config)
    except DockerUnavailableError:
        get_logger(__name__).error(
            "docker_unavailable", action="Offline Docker was requested; provision it manually or use restricted mode"
        )
        return 1


def _create_broker(config: GryphonConfig, registry: Registry) -> ToolBroker:
    """Load typed auth settings locally, without resolving tokens during startup."""
    from gryphon.compiler.orchestrator import Orchestrator, _to_module_name  # noqa: PLC0415
    from gryphon.security.broker import ToolBroker

    # Load auth configs from swaggers.yaml so dynamic token fetching (Keycloak, OAuth2, etc.)
    # is available at runtime without requiring GRYPHON_{SERVER}_AUTH env vars.
    sources = Orchestrator(config).load_swagger_sources()
    auth_configs: dict[str, AuthConfig] = {_to_module_name(s.name): s.auth for s in sources if s.auth is not None}
    return ToolBroker(config, registry, auth_configs=auth_configs)


async def _serve_started(args: argparse.Namespace, config: GryphonConfig) -> int:
    """Register cleanup before each startup step, including constructors that follow I/O."""
    from gryphon.runtime.cache import CacheStore  # noqa: PLC0415
    from gryphon.runtime.executor import CodeExecutor  # noqa: PLC0415
    from gryphon.runtime.registry import Registry  # noqa: PLC0415
    from gryphon.server import ActivityClock, create_server  # noqa: PLC0415

    async with AsyncExitStack() as stack:
        # Initialize cache
        cache = CacheStore(config.cache_db_path, config.cache_ttl_seconds, config.cache_max_entries)
        stack.push_async_callback(cache.close)
        await cache.initialize()
        await cache.cleanup_expired()
        # Load registry
        registry = Registry(config.compiled_output_dir)
        registry.load()
        broker = _create_broker(config, registry)
        stack.push_async_callback(broker.close)
        # Start the selected executor (restricted Monty by default).
        # Optional Docker is never auto-started and fails closed when unavailable;
        # no degraded server is substituted for the configured execution profile.
        executor = CodeExecutor(config, cache, registry, broker=broker)
        # Executor may only partially start; always shut down its partial state.
        stack.push_async_callback(executor.shutdown)
        try:
            await executor.startup()
        except CacheError:
            get_logger(__name__).error(
                "run_ledger_unavailable",
                action=(
                    "Run ledger lock is unsafe or unavailable; check permissions or set a distinct GRYPHON_STATE_DIR"
                ),
            )
            raise
        # Normal path — executor is fully started; ensure shutdown runs even on error
        # so no execution resources are left orphaned after serving.
        transport = getattr(args, "transport", "stdio")
        reap = transport == "stdio" and getattr(args, "command", None) == "stdio"
        activity = ActivityClock() if reap else None
        mcp = create_server(config, registry=registry, cache=cache, executor=executor, activity=activity)
        get_logger(__name__).info(
            "gryphon_starting", version=__version__, transport=transport, profile=config.sandbox_mode
        )
        if transport == "stdio":
            if activity is not None:
                await _serve_stdio(mcp, activity, config.stdio_idle_timeout_seconds)
            else:
                await mcp.run_stdio_async(show_banner=False)
        else:
            await mcp.run_http_async(host=config.host, port=config.port, show_banner=False)
        # Stop execution, close broker connections and cache in reverse startup order.
    return 0


async def _serve_stdio(mcp: Any, activity: ActivityClock, timeout_seconds: int) -> None:
    """Serve stdio and exit once an abandoned host stops sending messages.

    A host that drops a connection without closing the child's stdin leaves a process
    waiting forever; reaping it releases the run ledger and its resources. Cancellation
    cannot unwind the SDK's blocked reader, so reaping exits the process directly.
    """
    server = asyncio.create_task(mcp.run_stdio_async(show_banner=False))
    if timeout_seconds <= 0:
        await server
        return

    async def watchdog() -> None:
        """Return when no inbound MCP message has arrived within the configured window."""
        interval = min(60.0, float(timeout_seconds))
        while True:
            await asyncio.sleep(interval)
            if time.monotonic() - activity.last >= timeout_seconds:
                return

    watch = asyncio.create_task(watchdog())
    done, _ = await asyncio.wait({server, watch}, return_when=asyncio.FIRST_COMPLETED)
    if server in done:
        watch.cancel()
        with suppress(asyncio.CancelledError):
            await watch
        server.result()
        return
    get_logger(__name__).warning(
        "stdio_idle_reaped", action="No MCP client activity; exiting to release the run ledger"
    )
    os._exit(0)


async def _cmd_run(args: argparse.Namespace) -> int:
    """Compile exactly once and serve using the same validated settings."""
    from gryphon.cli_setup import prepare_stdio_state

    config = _config_for(args)
    if not _prepare_transport(args, config):
        return 1
    if args.command == "stdio":
        await asyncio.to_thread(prepare_stdio_state, config)
    if await _cmd_compile(args):
        return 1
    return await _cmd_serve(args, compiled=True)


async def _cmd_doctor(args: argparse.Namespace) -> int:
    """Write read-only diagnostics as JSON, without constructing runtime services."""
    from gryphon.cli_doctor import doctor_report

    sys.stdout.write(doctor_report(_config_for(args), getattr(args, "env_file", None)) + "\n")
    return 0


async def _cmd_saas(args: argparse.Namespace) -> int:
    """Start the hosted application with explicit environment-only operator settings."""
    from gryphon.saas import serve
    from gryphon.saas_config import SaaSConfig

    options: dict[str, Any] = {"_env_file": getattr(args, "env_file", None)}
    return await serve(SaaSConfig(**options), _config_for(args))


def main() -> None:
    """Run the Gryphon CLI with JSON command output on stdout and safe diagnostics on stderr.

    Raises:
        SystemExit: With the command's exit status, or a safe failure status.
    """
    parser = _build_parser()
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(0)
    if args.command == "stdio":
        _die_with_parent()
    setup_logging("INFO")
    try:
        # Load .env into os.environ early so vault.py can read server credentials.
        # override=False means explicit env vars always win over .env values.
        env_file_path = args.env_file if args.env_file is not None else str(_ENV_FILE)
        if args.command not in {"stdio", "saas"} or args.env_file is not None:
            load_dotenv(env_file_path, override=False)
        # Load config early for log level
        config = _config_for(args)
        setup_logging(config.log_level)
        command_map: dict[str, Callable[[argparse.Namespace], Coroutine[Any, Any, int]]] = {
            "clean": _cmd_clean,
            "compile": _cmd_compile,
            "serve": _cmd_serve,
            "run": _cmd_run,
            "stdio": _cmd_run,
            "saas": _cmd_saas,
            "doctor": _cmd_doctor,
        }
        exit_code = asyncio.run(command_map[args.command](args))
    except KeyboardInterrupt:
        exit_code = 130
    except Exception:  # noqa: BLE001
        get_logger(__name__).error("command_failed", action="Check configuration and local resources; details withheld")
        exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
