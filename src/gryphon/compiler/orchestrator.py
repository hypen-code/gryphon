"""Compile deterministic SDK documentation and complete v2 broker manifests."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from gryphon.compiler.catalog import contained_path, input_schema, load_manifest, module_name
from gryphon.compiler.client_config import ClientConfigSupport
from gryphon.compiler.codegen import CodeGenerator, _build_return_type
from gryphon.compiler.documents import DEFAULT_MAX_DOCUMENT_BYTES, fetch_document
from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.compiler.top_level_codegen import TopLevelFunctionGenerator
from gryphon.errors import CompileError, SwaggerFetchError
from gryphon.models import EndpointManifest, ServerManifest, ServerSpec, SwaggerSource
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from gryphon.config import GryphonConfig

logger = get_logger(__name__)
_to_module_name = module_name


class CompileResult:
    """Summary of successful, unchanged and rejected configured sources."""

    def __init__(self) -> None:
        """Initialize an empty compile result."""
        self.compiled: list[str] = []
        self.skipped: list[str] = []
        self.failed: list[str] = []
        self.total_endpoints: int = 0
        self.mcp_json: str | None = None


class Orchestrator(ClientConfigSupport):
    """Coordinate fail-closed catalog compilation without executing generated code."""

    def __init__(self, config: GryphonConfig, *, env_file: str | None = None) -> None:
        """Initialize trusted document policy and an optional client env-file path without reading it."""
        self._config = config
        self._env_file = env_file
        self._codegen = CodeGenerator()
        self._top_level_gen = TopLevelFunctionGenerator()
        self._output_dir = Path(config.compiled_output_dir)

    def load_swagger_sources(self) -> list[SwaggerSource]:
        """Load sources, rejecting malformed or normalization-colliding names without exposing auth."""
        if self._config.swaggers is not None:
            return self._validate_sources(self._config.swaggers)
        path = Path(self._config.swagger_config_file)
        if not path.exists():
            logger.warning("swagger_config_not_found")
            return []
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            raise CompileError("Failed to load swagger config") from None
        if raw is None:
            return []
        if not isinstance(raw, dict) or not isinstance(raw.get("servers", []), list):
            raise CompileError("Swagger config must contain a servers list")
        return self._validate_sources(raw.get("servers", []))

    @staticmethod
    def _validate_sources(entries: Sequence[object]) -> list[SwaggerSource]:
        """Apply identical source and name validation to environment and YAML inputs."""
        sources: list[SwaggerSource] = []
        names: set[str] = set()
        for entry in entries:
            try:
                source = SwaggerSource.model_validate(entry)
                name = module_name(source.name)
                if name in names:
                    raise CompileError("Configured server names collide after normalization")
                names.add(name)
                sources.append(source)
            except ValidationError:  # Never log Pydantic input values from credential-bearing config.
                logger.warning("invalid_swagger_source", reason="invalid configuration")
                raise CompileError("Invalid swagger source configuration") from None
        logger.info("swagger_sources_loaded", count=len(sources))
        return sources

    async def compile_all(self, dry_run: bool = False) -> CompileResult:
        """Compile configured sources, with dry-run making no filesystem writes."""
        if self._config.llm_enhance:
            raise CompileError("LLM enhancement is unsupported in v2; deterministic manifests are required")
        sources = self.load_swagger_sources()
        result = CompileResult()
        compiled_specs: dict[str, ServerSpec] = {}
        if not sources:
            logger.warning("no_swagger_sources_configured")
            return result
        contained_path(self._output_dir)
        if not dry_run:
            self._output_dir.mkdir(parents=True, exist_ok=True)
        for source in sources:
            try:
                count, spec = await self._compile_source(source, dry_run)
                compiled_specs[source.name] = spec
                if count:
                    result.compiled.append(source.name)
                    result.total_endpoints += count
                else:
                    result.skipped.append(source.name)
            except (CompileError, SwaggerFetchError, OSError, ValueError, TypeError):  # Never log source values.
                logger.error("compile_failed", server=module_name(source.name), reason="invalid or unsupported source")
                result.failed.append(source.name)
        if not dry_run:
            self._lint_all_generated_code()
            result.mcp_json = self._generate_mcp_json(sources, compiled_specs)
        logger.info(
            "compile_complete", compiled=len(result.compiled), skipped=len(result.skipped), failed=len(result.failed)
        )
        return result

    async def _compile_source(self, source: SwaggerSource, dry_run: bool) -> tuple[int, ServerSpec]:
        """Regenerate when any schema, artifact, source policy or compiler contract differs."""
        module = module_name(source.name)
        server_dir = contained_path(self._output_dir, module)
        manifest_path = contained_path(self._output_dir, module, "manifest.json")
        # Parse the swagger document (resolves base_url from spec if not set in swaggers.yaml)
        spec = await SwaggerParser(source, config=self._config).parse()
        # Keep resolved base_url in the spec; never mutate trusted source configuration.
        code = self._codegen.generate(spec)
        expected = self._build_manifest(module, spec, self._source_hash(source))
        if dry_run:
            logger.info("dry_run_parsed", server=module, endpoints=len(spec.endpoints))
            return len(spec.endpoints), spec
        # Fetch skills content independently of code-generation state
        skills: str | None = None
        if source.skills_url:
            skills = await self._fetch_skills_content(
                source.skills_url,
                module,
                self._config.max_spec_size_bytes,
                config=self._config,
            )
        # Check if code recompile is needed
        current = self._is_up_to_date(manifest_path, spec.swagger_hash, expected, code)
        # Code may be current; still refresh skills and SDK wrappers so configuration
        # changes, including removing all selected wrappers, cannot preserve active tools.
        server_dir.mkdir(parents=True, exist_ok=True)
        self._write_skills(server_dir, skills, module)
        self._write_top_level_functions(server_dir, spec, module, source.top_level_functions)
        if current:
            logger.info("server_up_to_date", server=module)
            return 0, spec
        # Generate code
        compile(code, "<generated-sdk>", "exec")
        # Write output
        self._write_functions(server_dir, spec, code)
        self._write_manifest(server_dir, spec, expected.template_hash)
        logger.info("server_compiled", server=module, endpoints=len(spec.endpoints))
        return len(spec.endpoints), spec

    @staticmethod
    async def _fetch_skills_content(
        skills_url: str,
        server_name: str,
        max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
        *,
        config: GryphonConfig | None = None,
    ) -> str | None:
        """Fetch optional bounded skills through the same trusted network policy as specifications."""
        try:
            return await fetch_document(skills_url, max_bytes, config=config)
        except SwaggerFetchError:
            logger.warning("skills_fetch_failed", server=server_name, reason="document unavailable or unsafe")
            return None

    @staticmethod
    def _safe_write(server_dir: Path, filename: str, content: str) -> None:
        """Write contained ordinary files and refuse final-component symlinks."""
        path = contained_path(server_dir, filename)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)

    @staticmethod
    def _write_skills(server_dir: Path, content: str | None, server_name: str) -> None:
        """Refresh documentation while retaining it on transient fetch failures."""
        if content is not None:
            Orchestrator._safe_write(server_dir, "skills.md", content)
            logger.debug("skills_written", server=server_name)

    def _write_top_level_functions(
        self,
        server_dir: Path,
        spec: ServerSpec,
        module_name: str,
        top_level_names: list[str],
    ) -> None:
        """Overwrite stale SDK wrapper registries even when all selections are removed."""
        code = self._top_level_gen.generate(spec, module_name, top_level_names)
        if code is None:
            code = '"""No promoted SDK wrappers; the v2 host does not load generated modules."""\n'
            code += "from __future__ import annotations\n\nfrom typing import Any\n\n"
            code += "_TOP_LEVEL_TOOLS: list[dict[str, Any]] = []\n"
        self._safe_write(server_dir, "top_level_functions.py", code)

    def _is_up_to_date(
        self,
        manifest_path: Path,
        current_hash: str,
        expected: ServerManifest | None = None,
        code: str | None = None,
    ) -> bool:
        """Require matching complete native metadata, SDK source, source policy and compiler."""
        contained_path(self._output_dir, manifest_path.parent.name, manifest_path.name)
        if not manifest_path.exists():
            return False
        try:
            manifest = load_manifest(manifest_path)
            if manifest.swagger_hash != current_hash:
                return False
            if expected is None:
                return manifest.template_hash == self._template_hash()
            if manifest.model_dump(exclude={"compiled_at"}) != expected.model_dump(exclude={"compiled_at"}):
                return False
            functions = contained_path(self._output_dir, manifest_path.parent.name, "functions.py")
            init = contained_path(self._output_dir, manifest_path.parent.name, "__init__.py")
            return functions.is_file() and init.is_file() and functions.read_text(encoding="utf-8") == code
        except (CompileError, OSError, ValueError):
            return False

    def _write_functions(self, server_dir: Path, spec: ServerSpec, code: str) -> None:
        """Write SDK source and a literal-safe module docstring; never import either."""
        self._safe_write(server_dir, "functions.py", code)
        self._safe_write(server_dir, "__init__.py", ascii(f"Auto-generated Gryphon SDK for {spec.name}.") + "\n")

    def _build_manifest(self, name: str, spec: ServerSpec, template_hash: str) -> ServerManifest:
        """Build complete endpoint metadata with native input and successful output JSON schemas."""
        endpoints = [
            EndpointManifest(
                function_name=ep.operation_id,
                summary=ep.summary,
                description=ep.description,
                method=ep.method,
                path=ep.path,
                parameters_summary=", ".join(
                    f"{p.name} ({p.param_type}, {'required' if p.required else 'optional'})" for p in ep.parameters
                ),
                response_summary=", ".join(field.name for field in ep.response_schema) or "response data",
                return_type=_build_return_type(ep),
                parameters=ep.parameters,
                response_fields=ep.response_schema,
                request_body_schema=ep.request_body_schema,
                request_body_media_type=ep.request_body_media_type,
                read_only_post=ep.read_only_post,
                base_url=ep.base_url or spec.base_url,
                input_schema=input_schema(ep),
                output_schema=ep.response_json_schema,
            )
            for ep in spec.endpoints
        ]
        return ServerManifest(
            format_version=2,
            server_name=name,
            description=spec.description,
            swagger_hash=spec.swagger_hash,
            template_hash=template_hash,
            compiled_at=datetime.now(tz=UTC).isoformat(),
            base_url=spec.base_url,
            is_read_only=spec.is_read_only,
            endpoints=endpoints,
        )

    def _write_manifest(self, server_dir: Path, spec: ServerSpec, template_hash: str | None = None) -> None:
        """Publish native structured metadata instead of lossy signature summaries."""
        manifest = self._build_manifest(server_dir.name, spec, template_hash or self._template_hash())
        self._safe_write(server_dir, "manifest.json", manifest.model_dump_json(indent=2))

    def _lint_all_generated_code(self) -> None:
        """Lint SDK source without executing it or logging untrusted source text."""
        generated = sorted(self._output_dir.glob("*/functions.py"))
        generated += sorted(self._output_dir.glob("*/top_level_functions.py"))
        if not generated:
            return
        for path in generated:
            contained_path(self._output_dir, path.parent.name, path.name)
        try:
            result = subprocess.run(  # noqa: S603
                [sys.executable, "-m", "ruff", "check", "--quiet", *map(str, generated)],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode:
                logger.warning("generated_code_lint_warnings", files=len(generated))
            else:
                logger.info("generated_code_lint_passed", files=len(generated))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            logger.warning("lint_skipped", reason="linter unavailable")
