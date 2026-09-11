# Gryphon Changelog

Notable changes to Gryphon, following
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0]

### Added

- Credential-free restricted Python execution using `pydantic-monty` 0.0.18,
  with a fresh VM, bounded resources, and a single two-argument capability:
  `await call_tool("server.function", arguments)`.
- A host-owned capability broker for schema-validated API calls, credential
  resolution, read-only enforcement, and exact administrator write permits.
- DNS-pinned, origin-pooled HTTP connections with TLS verification, disabled
  environment proxies/redirects, bounded responses, and metadata-address denial.
- Six additional core meta-tools: `search_functions`, `submit_code`, `get_run`,
  `cancel_run`, `list_recipes`, and `read_artifact`, bringing the core set to ten
  while retaining `reusable_code_guide` as the reusable-program prompt.
- Bounded discovery/inspection with catalog fingerprints, native structured MCP
  results, and owner-scoped artifacts readable in chunks of at most 8192 bytes.
- Persistent owner-scoped run receipts, atomic retained-request idempotency,
  bounded admission, cancellation, and interrupted-state recovery under exclusive
  single-process run-ledger ownership.
- Required HTTP bearer authentication with a minimum 32-character `SecretStr`
  setting and a fixed verified operator identity, distinct from local stdio.
- Read-only `gryphon doctor` diagnostics and a `--version` command.
- A credential-free Open-Meteo example catalog and a real in-process MCP demo
  that performs offline computation and structured recipe reuse without a model.
- Modern MCP 2026-07-28 and legacy-initialization conformance tests, plus opt-in
  benign offline Docker transport/library checks.

### Changed

- Branding is **Gryphon**, the legendary guardian: distribution `gryphon-runtime`,
  import package and CLI `gryphon`, environment prefix `GRYPHON_`. The existing
  GitHub repository URL is unchanged.
- Version 2.0.0 is installed **from the checkout**, not a published PyPI package.
  Python 3.13+ remains required; FastMCP is pinned to exactly 4.0.2. `uv.lock`,
  CI, container builds, and local hooks support locked environments.
- Restricted execution is the default. Optional full-Python Docker is a separate
  **offline** profile: no credentials, network, API broker, or host mounts; fresh
  non-root containers with resource limits and `runsc` selected by default.
  Missing daemon, image, or isolation runtime fails closed without fallback.
- Dynamic values are explicit JSON `inputs`; replay `params` replaces the full
  input object. Optional input schemas are bounded and reject references,
  regexes, and combinators. Input values are never substituted into source.
- Recipe identity now binds owner, exact source, input schema, and catalog/policy
  digest. Replays rerun guards and authorization and reject stale identities.
- Compiler output includes deterministic v2 manifests with request/body schemas,
  encoding metadata, source policy, and bounded response descriptions. Unsupported
  request semantics fail closed. Generated SDK source is not host execution code.
- Credentials and dynamic token refresh stay in the trusted host broker/vault,
  never either sandbox. Writes default off and need both the global switch and
  exact `GRYPHON_ALLOWED_WRITE_OPERATIONS` permits in addition to source policy.
- Raw prints, stderr, upstream failure bodies, and traces are omitted from public
  execution output; successful large data uses bounded owner-scoped artifacts.
- `serve` respects `compile_on_startup`; `run` compiles once before serving.
  `--env-file` works before or after the subcommand. HTTP defaults to loopback.
- `clean --yes` archives only recognized generated output and a closed recipe
  cache after the operator stops the server. Unsafe/unknown targets are refused;
  run receipts, artifacts, and configuration are retained.
- Optional skills guides are untrusted, bounded, and fetched on demand through
  `list_skills`/`get_server_skills`, never embedded as initialization authority.
- Compose uses a non-root restricted-profile service, required auth, named storage
  volumes, resource limits, and no Docker socket. Its health check is TCP only.
- Strict typing covers source and tests. The mandatory **90% coverage floor**
  remains in place, with **100%** as the target and locked local pre-commit hooks.

### Fixed

- Standalone compilation prints credential-free MCP client JSON even for unchanged
  catalogs; startup compilation remains silent on stdout. Generated entries pin
  storage/env-file paths and disable recompilation from the client's directory.
- Preserve `sandbox_unavailable` through MCP error sanitization and nested run
  receipts instead of misclassifying Docker failures as internal errors.
- Canonicalize exact write-permit sets for replay/idempotency identity without
  changing authorization; reordered or duplicated permits no longer cause drift.
- Isolate shared test settings from operator dotenv files. Add actual CLI
  compile/stdio/demo smoke coverage and concurrent idempotency verification.

### Removed or retired

- Host execution of generated/promoted endpoint tools. Legacy
  `top_level_functions` selections do not register direct MCP API tools.
- Automatic `main()` invocation and source-rewriting parameter injection.
- Credential-bearing, network-enabled, or shared warm Docker execution.
- LLM-enhanced compilation: `--llm-enhance` and enabling its setting are explicitly
  rejected. Deterministic compilation requires no model provider key.
- Automatic skills embedding and unbounded static guide resources.

### Operational limits

- Run receipts are portable application handles, **not native MCP Tasks**;
  the Tasks extension is neither implemented nor advertised.
- Persisted queued/running work becomes `interrupted` on restart. No automatic
  crash resume or write replay occurs, and no exactly-once external effect is
  promised. Cancellation cannot undo API actions already accepted upstream.
- Owner-scoped storage and fixed operator auth support a local/operator profile,
  not a completed multi-tenant SaaS. Public exposure requires TLS termination and
  appropriate additional deployment controls.

## [0.1.0] — 2026-03-14

Initial public release. This section records historical Gryphon behavior;
features superseded above are not instructions for running version 2.0.0.

### Added

- Four MCP meta-tools: `list_servers`, `get_functions`, `execute_code`, and
  `run_cached_code`; one `reusable_code_guide` prompt.
- Swagger/OpenAPI compilation to typed Python functions using Jinja2 templates.
- Docker-based execution with memory/CPU limits, a non-root user, and timeouts,
  backed by an AST guard for dangerous imports and calls.
- A credential vault whose initial execution design used Docker environment
  injection; **replaced by host-only broker credential handling in v2**.
- Async SQLite code caching with TTL/LRU and reusable-code execution with changed
  parameters; **replaced by structured-input, owner/policy-bound recipes in v2**.
- Optional directly promoted endpoint tools and server skills resources;
  **direct host tools are disabled and guides are on demand in v2**.
- Optional LLM docstring/example enhancement; **retired and rejected in v2**.
- Domain allowlists and compile-time read-only method filtering.
- `gryphon compile`, `serve`, `run`, and `clean` commands.
- CI and local hooks for Ruff, strict mypy, tests, and a 90% coverage gate.

### Removed

- `get_cached_code`, superseded by returning a `cache_id` from `execute_code`
  for subsequent `run_cached_code` calls.
