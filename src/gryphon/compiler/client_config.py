"""Non-secret client hints and deterministic v2 compilation fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gryphon.compiler.catalog import contained_path, module_name

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.models import ServerSpec, SwaggerSource


class ClientConfigSupport:
    """Provide placeholders without resolving or serializing credentials."""

    _output_dir: Path
    _config: GryphonConfig
    _env_file: str | None

    @staticmethod
    def _template_hash() -> str:
        """Hash compiler implementation, templates and shared model contracts."""
        compiler = Path(__file__).parent
        paths = sorted(compiler.glob("*.py")) + sorted((compiler / "templates").glob("*.j2"))
        paths.append(compiler.parent / "models" / "__init__.py")
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _source_hash(self, source: SwaggerSource) -> str:
        """Hash relevant compilation/fetch policy without recording credential values."""
        policy = {
            "name": module_name(source.name),
            "base_url": source.base_url,
            "swagger_url": source.swagger_url,
            "is_read_only": source.is_read_only,
            "top_level_functions": sorted(source.top_level_functions),
            "skills_url": source.skills_url,
            "max_spec_size_bytes": self._config.max_spec_size_bytes,
            "allowed_domains": sorted(self._config.allowed_domains),
            "allow_private_networks": self._config.allow_private_networks,
        }
        return hashlib.sha256((self._template_hash() + json.dumps(policy, sort_keys=True)).encode()).hexdigest()

    def _find_latest_server_dir(self) -> Path | None:
        """Find the newest contained compiled manifest for client-config availability."""
        if not self._output_dir.exists():
            return None
        candidates: list[tuple[float, str, Path]] = []
        for directory in sorted(self._output_dir.iterdir()):
            if directory.is_dir():
                path = contained_path(self._output_dir, directory.name, "manifest.json")
                if path.is_file():
                    candidates.append((path.stat().st_mtime, directory.name, directory))
        return max(candidates)[2] if candidates else None

    @staticmethod
    def _resolve_gryphon_command(compiled_output_dir: Path) -> str:
        """Resolve the installed CLI without executing project-discovered binaries."""
        candidate = compiled_output_dir.resolve()
        for _ in range(6):
            executable = candidate / ".venv" / "bin" / "gryphon"
            if executable.exists():
                return str(executable)
            candidate = candidate.parent
        # Fallback: same bin directory as the running Python interpreter
        return str(Path(sys.executable).parent / "gryphon")

    @staticmethod
    def _auth_env_hints(server_name: str, auth: Any) -> dict[str, str]:
        """Return fixed placeholders or validated environment references, never auth values."""
        from gryphon.models import (  # noqa: PLC0415
            BasicAuthConfig,
            JwtAuthConfig,
            KeycloakAuthConfig,
            OAuth2AuthConfig,
            SessionAuthConfig,
            StaticAuthConfig,
        )

        prefix = f"GRYPHON_{module_name(server_name).upper()}_"
        if isinstance(auth, (StaticAuthConfig, JwtAuthConfig)):
            return {f"{prefix}AUTH": f"${{{prefix}AUTH}}"}
        fields: list[str] = []
        if isinstance(auth, (OAuth2AuthConfig, KeycloakAuthConfig)):
            fields.append(auth.client_secret)
        elif isinstance(auth, (BasicAuthConfig, SessionAuthConfig)):
            # Never compute a token at compile time — only emit validated variable refs.
            # The user configures these variables on the gryphon serve process.
            fields.extend([auth.username, auth.password])
        else:
            return {}
        hints: dict[str, str] = {}
        for field in fields:
            for name in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", field):
                hints[name] = f"${{{name}}}"
        if hints:
            return hints
        # Literal credentials are never computed or baked into the MCP JSON.
        return {f"{prefix}AUTH": f"${{{prefix}AUTH}}"}  # literal secret — emit a placeholder only

    def _generate_mcp_json(self, sources: list[SwaggerSource], compiled_specs: dict[str, ServerSpec]) -> str | None:
        """Build client configuration without auth or extra-header values."""
        if not compiled_specs or self._find_latest_server_dir() is None:
            return None
        env = {
            "GRYPHON_COMPILE_ON_STARTUP": "false",
            "GRYPHON_COMPILED_OUTPUT_DIR": str(self._output_dir.resolve()),
            "GRYPHON_SWAGGER_CONFIG_FILE": str(Path(self._config.swagger_config_file).resolve()),
            "GRYPHON_CACHE_DB_PATH": str(Path(self._config.cache_db_path).resolve()),
            "GRYPHON_RUN_DB_PATH": str(Path(self._config.run_db_path).resolve()),
            "GRYPHON_ARTIFACT_DIR": str(Path(self._config.artifact_dir).resolve()),
            "GRYPHON_ENABLE_ADDITIONAL_TOOLS": "false",
        }
        for source in sources:
            if self._env_file is not None or source.name not in compiled_specs:
                continue
            # Sanitize to match the module directory name and vault's env var prefix.
            # "cse-api" → "CSE_API" so generated code and MCP JSON are consistent.
            prefix = module_name(source.name).upper()
            # Extra destination variables are placeholders; compilation fixes broker destinations.
            spec = compiled_specs[source.name]
            for variable in ["BASE_URL", *(name.lstrip("_") for name in spec.server_url_vars.values())]:
                key = f"GRYPHON_{prefix}_{variable}"
                env[key] = f"${{{key}}}"
            if source.auth is not None:
                env.update(self._auth_env_hints(source.name, source.auth))
            if source.extra_headers:
                key = f"GRYPHON_{prefix}_EXTRA_HEADERS"
                env[key] = f"${{{key}}}"
        args = ["serve"]
        if self._env_file is not None:
            args.extend(["--env-file", str(Path(self._env_file).resolve())])
        entry = {"command": self._resolve_gryphon_command(self._output_dir), "args": args, "env": env}
        return json.dumps({"mcpServers": {"gryphon": entry}}, indent=2)
