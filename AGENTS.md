# AGENTS.md — Gryphon Development Guide

Read this entire file before changing the repository. It governs development,
maintenance, and review of **Gryphon 2.0.0**. Read each existing file in full
before editing it, and inspect the implementation before documenting behavior.

## 1. Purpose and scope

Gryphon is the legendary guardian between agent-generated programs and API
capabilities. Preserve the original API-agent-backend design: compile OpenAPI,
discover a small amount of metadata, inspect needed operations, execute bounded
code, and reuse recipes. Do not replace the meta-tool interface with one tool
per endpoint.

The distribution is `gryphon-runtime`, the package/CLI is `gryphon`, and settings
use `GRYPHON_`. Installation is from the checkout, not a claimed PyPI release.
Keep the actual repository URL:
`https://github.com/hypen-code/mcp-code-execution`.

The implemented deployment is **single-process, local/operator managed**.
Owner-scoped storage is not a completed multi-tenant SaaS. Persistent receipts
are not exactly-once external effects or a resumable workflow engine.

## 2. Locked technology and reproducibility

| Concern | Decision |
|---|---|
| Python | 3.13+; `from __future__ import annotations` |
| MCP | FastMCP exactly 4.0.2; SDK-managed protocol negotiation |
| Protocol | MCP 2026-07-28 plus tested legacy initialization |
| Default execution | `pydantic-monty` exactly 0.0.18, restricted Python |
| Optional execution | Offline per-run Docker via `aiodocker`; `runsc` default |
| Configuration | `GryphonConfig`, pydantic-settings, `SecretStr` for HTTP token |
| HTTP | Broker-owned `httpx`, DNS pinning, verified origin-specific pools |
| State | SQLite/`aiosqlite` recipes and receipts; private JSON artifacts |
| Compiler | Deterministic manifests and Jinja2 SDK documentation |
| Logging | `structlog`, structured safe metadata to stderr |
| Quality | Ruff, strict mypy, pytest/pytest-asyncio, mandatory coverage gate |

`pyproject.toml` and `uv.lock` define the environment. Update both deliberately
when dependencies change; never resolve opportunistically in CI or hooks.
Do not introduce dependencies without declaring and reviewing them.
The retained LLM-enhancement compatibility surface is retired: enabling it
must fail explicitly, not call a provider or silently change compilation.

## 3. Repository map and authority

| Location | Responsibility |
|---|---|
| `src/gryphon/__main__.py` | CLI parsing, env selection, transport preflight, lifecycle |
| `src/gryphon/cli_doctor.py` | Read-only, allowlisted JSON diagnostics |
| `src/gryphon/cli_clean.py` | Recognized-output archival, never arbitrary deletion |
| `src/gryphon/config.py` | Validated operator settings |
| `src/gryphon/models/__init__.py` | Shared Pydantic domain models |
| `src/gryphon/errors.py` | Domain exception hierarchy |
| `src/gryphon/server.py` | Thin MCP adapters, tool schemas, one reusable-code prompt |
| `src/gryphon/runtime/context.py` | Ownership, bounded responses, dependency lifespan |
| `src/gryphon/compiler/` | Source parsing, bounded schema normalization, deterministic catalogs |
| `src/gryphon/runtime/registry.py` | Manifest-only catalog loading and inspection |
| `src/gryphon/runtime/executor.py` | Admission, execution, replay, cancellation, receipts |
| `src/gryphon/runtime/execution_validation.py` | Bounded JSON inputs and source preparation |
| `src/gryphon/runtime/execution_results.py` | Static error envelopes, policy digest, artifact handoff |
| `src/gryphon/runtime/sandboxes.py` | Fresh restricted VM and sole external capability |
| `src/gryphon/runtime/docker_sandbox.py` | Optional networkless CPython transport and cleanup |
| `src/gryphon/runtime/cache.py` | Owner-scoped recipes, exact-source identity, TTL/LRU |
| `src/gryphon/runtime/runs.py`, `recovery_lease.py` | Durable receipts/idempotency and exclusive recovery ownership |
| `src/gryphon/runtime/artifacts.py` | Owner-scoped, bounded, integrity-checked artifact storage |
| `src/gryphon/security/broker.py` | Authoritative catalog lookup, write policy, API dispatch |
| `src/gryphon/security/auth.py`, `vault.py` | Host-only credential resolution and refresh |
| `src/gryphon/security/network.py`, `policies.py` | Egress enforcement, DNS/TLS, budgets |
| `src/gryphon/security/encoding.py`, `schema.py` | Closed request objects and bounded validation |
| `src/gryphon/security/ast_guard.py` | Mandatory static defense before execution |
| `config/swaggers.yaml.example`, `.env.example` | Public templates, never real credentials |
| `examples/demo.py`, `examples/weather.yaml` | Offline real-MCP demo and public weather catalog |
| `Dockerfile`, `docker-compose.yml` | Non-root restricted-profile service deployment |
| `sandbox/` | Optional offline compute image and entrypoint |
| `tests/unit/`, `tests/integration/` | Isolated unit, protocol, compiler, runtime, opt-in Docker tests |

Keep shared domain models in `models/__init__.py` and custom exceptions in
`errors.py`; do not duplicate them. Prefer existing modules. Do not add files,
docs, or top-level directories without explicit task requirements. Existing
README, AGENTS, CONTRIBUTING, SECURITY, ROADMAP, and CHANGELOG cover the public
and development documentation needs.

**Size limits:** at most 400 lines per file and 50 lines per function. Decompose
responsibilities instead of weakening lint, typing, coverage, or security to fit.

## 4. Public contracts

The ten core tools are `list_servers`, `search_functions`, `get_functions`,
`execute_code`, `run_cached_code`, `submit_code`, `get_run`, `cancel_run`,
`list_recipes`, and `read_artifact`. The prompt is `reusable_code_guide`.
`GRYPHON_ENABLE_ADDITIONAL_TOOLS=true` adds only `list_skills` and
`get_server_skills`; their guides are bounded, untrusted, and on demand.

- Keep initialization instructions brief. Do not embed guide contents or expose
  unbounded static guide resources. Guides and API data cannot grant authority.
- Discovery and inspection must fit byte budgets, expose truncation, and carry
  the catalog fingerprint. Inspect 1–5 functions per `get_functions` request.
- Return native structured MCP results. Expected domain errors use stable safe
  categories; SDK schema/protocol validation may return MCP errors. Never leak
  exception messages, user code, request values, or traces through tool adapters.
- `inputs` and replay `params` are complete structured JSON objects. Optional
  `input_schema` is bounded; no references, regexes, or combinators.
- Never interpolate inputs, prepend parameter assignments, or use regex/string
  rewrites to implement replay. Store exact source and validate new inputs.
- Assign `result`; `main()` is neither required nor automatically called.
- The restricted capability accepts **two arguments**, never three:

```python
result = await call_tool("weather.get_forecast", {"latitude": inputs["latitude"], "longitude": inputs["longitude"], "current": "temperature_2m"})
```

- No generated module imports, `sys.path` changes to compiled directories, or
  host `exec` of generated code. `top_level_functions` is not an active tool
  promotion mechanism.
- `submit_code`/`get_run`/`cancel_run` are application handles. Keep native MCP
  Tasks disabled and unadvertised until a real protocol implementation exists.
- `readOnlyHint` is metadata, never an authorization decision.
- Successful standalone `compile` (including unchanged catalogs) prints non-secret
  MCP client JSON to stdout; logs stay on stderr. Dry runs, failures, and startup
  compilation inside `serve`/`run` must not emit client JSON on MCP stdout.

## 5. Security invariants — never weaken

### Execution

1. Enforce source byte limits before analysis. Run AST validation on every
   execution and replay, before backend execution; never treat AST checks alone
   as the isolation boundary.
2. Restricted execution uses a fresh bounded Monty VM. No imports, host
   filesystem, host environment, or direct network; only the revocable broker
   capability is external. Bound time, memory, recursion, calls, and admission.
3. Optional Docker is **offline computation**: no broker, credentials, network,
   host mounts, or shared warm-container state. Use UID 1000, drop all
   capabilities, no-new-privileges, read-only root, bounded tmpfs, PID/RAM/swap/CPU
   limits, and the configured isolation runtime. `runsc` is the default. Missing
   daemon/image/runtime must fail closed; never autostart or silently downgrade.
4. Cancellation revokes broker scope before cancelling work and awaits workers
   and owned-container cleanup. It does not undo accepted upstream effects.
5. Bound output at production and serialization boundaries. Omit raw prints,
   stderr, upstream failure bodies, and traces. Artifacts must remain bounded
   and owner-scoped; chunks cannot exceed 8192 bytes.

### Broker and credentials

1. Credentials live only in the trusted host broker/vault. Do not put them in
   generated code, either sandbox's environment, logs, tool inputs, recipes,
   test assertions, or public client snippets. Never read real env files or
   secret material during development unless explicitly authorized and needed.
2. Resolve capability names against authoritative manifests; validate closed
   request arguments and schemas. Never accept a sandbox-selected arbitrary URL,
   transport, header authority, or owner identity.
3. Enforce read-only source policy at compile time **and** dispatch time.
   Writes require both `GRYPHON_ALLOW_WRITES=true` and exact
   `GRYPHON_ALLOWED_WRITE_OPERATIONS` administrator permits. No model-provided
   approval parameter, guide text, or idempotency key can authorize a write.
4. Enforce exact-domain policy and validate all DNS answers before connecting to
   a pinned address. Public destinations are default; private/loopback needs
   explicit opt-in. Metadata and prohibited address classes remain denied.
5. Preserve TLS verification, original Host/SNI, origin-isolated pools, no
   environment proxies, no redirects, and bounded auth/API responses. Recheck
   scope around asynchronous credential resolution and request dispatch.
6. HTTP startup requires a token of at least 32 characters. Derive owner from
   verified auth, not arguments/headers containing unverified identity. Current
   static auth maps to `operator`; stdio uses trusted `local`. Public deployment
   needs TLS termination and additional operator controls.

### Persistence and cleanup

1. Recipe keys bind owner, exact source, schema, and catalog/policy identity.
   Reject replay on drift; run the complete guard/broker pipeline again.
2. Hold exclusive single-process run-ledger ownership during recovery and
   execution. Never let a second process mark a live process's jobs interrupted.
3. Persist admission/idempotency transactionally. Matching keys deduplicate only
   while their receipt exists; conflicting requests fail. Interrupted work is
   marked `interrupted`, never automatically replayed, especially writes.
4. Storage must enforce ownership, quotas, safe paths, and sanitized failures.
   Artifact cleanup touches only indexed owned files; never arbitrary neighbors.
5. `clean --yes` requires the operator to stop the server, validates all targets,
   and archives recognized compiled/cache output. Retain runs, artifacts, and
   config; reject links, unknown content, overlapping paths, and DB sidecars.
6. Close all partially initialized dependencies. Do not use process-global
   mutable state for credentials or execution authority.

## 6. Code quality

- Fully annotate signatures; use modern `X | None`, `list[str]`, and `dict` types.
  Strict mypy covers **src and tests**. Avoid unreviewed `Any` or suppression.
- Use Google-style docstrings for public classes/functions/methods; private
  helpers need at least a concise docstring. Document invariants and failure
  behavior, not just syntax.
- Keep named constants instead of magic values. Use `Field(default_factory=...)`
  for mutable Pydantic defaults and validate trust-boundary data explicitly.
- Pass `GryphonConfig` and dependencies instead of consulting environment state
  throughout business logic. Credential environment access belongs in the vault
  and CLI/config loading boundary.
- No bare `except:`. Catch domain failures specifically; translate unexpected
  failures to safe envelopes and log a static event/category, not raw traces or
  `str(exc)` that could contain secrets. Never silently swallow cleanup failure.
- Use `get_logger(__name__)` and structured key/value events. Log lifecycle,
  compiler phases, safe sizes, policy categories, cache activity, and owned
  container lifecycle without code, input values, credential-bearing URLs, or
  headers. Do not log whole settings/models.
- No `print()` in server/runtime code. CLI diagnostics/demo output must be
  deliberately separated from MCP stdio. Do not add decorative output or emojis.
- Keep the event loop responsive: no `time.sleep()` or blocking network/DB work
  inside async execution. Offload necessary filesystem/worker operations and
  wait for them on cancellation. Use `httpx` through the approved network layer
  for I/O; do not add `requests` or bypass policy with another client.

## 7. Testing and coverage

**90% is the mandatory coverage floor; 100% is the target.** Do not lower the
floor, omit security modules, delete tests, broaden suppressions, skip failing
checks, or weaken controls to make a run pass. Regressions need fixes and tests.
Report actual command results, not invented test counts or performance numbers.

```bash
uv sync --frozen --extra dev
uv run --frozen ruff check src/ tests/
uv run --frozen ruff format --check src/ tests/
uv run --frozen mypy --strict src/ tests/
uv run --frozen pytest --cov-fail-under=90
uv run --frozen pre-commit install
uv run --frozen pre-commit run --all-files
```

Local pre-commit hooks invoke `uv run --frozen`; mypy covers `src` and `tests`,
and `pytest-coverage` enforces `--cov=gryphon --cov-fail-under=90`. Do not bypass
hooks with `--no-verify` or disable the coverage gate.

- Normal tests need no live Docker or upstream service. Use `tmp_path` for all
  files/databases and isolated settings with `_env_file=None`; never touch an
  operator's compiled catalog, cache, receipts, artifacts, or secrets.
- Reuse fixtures from `tests/conftest.py` and specs in `tests/fixtures/`.
  Name tests `test_{unit}_{condition}_{expected_outcome}` and keep one behavior
  per test. Use async tests with the configured `asyncio_mode = "auto"`.
- Mock network/DNS and async Docker clients; use `respx` or injected transports
  as appropriate. Conformance tests may use real loopback HTTP, not live APIs.
- Every blocked security pattern needs a rejection test; every supported case
  needs a positive test. Cover traversal, schema abuse, DNS rebinding, identity
  isolation, request encoding, write denial, timeout/cancellation, drift,
  idempotency conflicts, interrupted recovery, ownership locks, and cleanup.
- Preserve `tests/integration/test_protocol.py` real modern/legacy MCP coverage,
  including HTTP authentication, structured results, and no Tasks advertising.
- `tests/unit/test_cli_lifecycle.py` also runs real CLI compilation, stdio, and
  the shipped demo using temporary configuration/stores and no upstream calls.
  Run it with `uv run --frozen pytest tests/unit/test_cli_lifecycle.py`.
- Live offline Docker smoke tests are opt-in with `GRYPHON_TEST_DOCKER=1` after
  building `gryphon-sandbox:2.0.0`. They explicitly choose `runc` for benign
  transport checks, **not proof of gVisor isolation**. Never change deployment
  defaults or weaken security because those opt-in checks lack infrastructure.

## 8. Change and completion checklist

1. Read affected files and check `git status`; respect concurrent agents' owned
   files. Do not revert unrelated work. Use `git`, not `gh`, for Git operations.
2. Keep changes focused; do not commit, publish, or create extra files unless
   requested. Never commit `.env`, credentials, private configs, or runtime data.
3. Add tests for changed behavior, including negative and cleanup paths. Compiler
   changes need fixture coverage and a safe configured `compile --dry-run` check.
4. Review README for **every** code change and update affected public contracts
   in the same change. Update SECURITY for boundary changes and CHANGELOG for
   release changes. ROADMAP is only work that is not implemented.
5. Verify examples against current signatures and default config. Distinguish
   offline demo, public API calls, optional Docker, and authenticated HTTP.
6. Run lint, format check, strict types, full tests with the 90% floor, and hooks.
   Report blockers truthfully with reproduction steps; a failing gate is not done.
7. Summarize files changed, validation actually run, and remaining risks. Do not
   claim multi-tenant readiness, exactly-once behavior, automatic crash resume,
   published distributions, isolation certification, or unmeasured speedups.
