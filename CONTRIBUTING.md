# Contributing to Gryphon

Gryphon helps AI agents discover APIs, compose bounded programs, and reuse them
without exposing credentials to their execution environment. Contributions
should preserve that purpose and the small meta-tool interface.

Read [AGENTS.md](AGENTS.md) for architecture and coding rules,
[SECURITY.md](SECURITY.md) for trust boundaries, and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before participating.

## Set up a checkout

You need Git, [uv](https://docs.astral.sh/uv/), and Python **3.13+**. Use
Linux/macOS for native development; persistent-store locking uses POSIX APIs.
Windows users can run the container deployment. Docker is optional for the
normal test suite and default restricted execution profile. Install system
**libpq** (Debian/Ubuntu: `libpq5`); the `saas` extra uses pure `psycopg==3.2.9`.
Default test collection imports hosted modules, so include `--extra saas` even
when no live PostgreSQL service is used.

```bash
git clone https://github.com/hypen-code/gryphon.git
cd gryphon
uv sync --frozen --extra dev --extra saas
uv run --frozen --extra saas pre-commit install
```

This installs the checkout's `gryphon-runtime` distribution and `gryphon` CLI.
Version 2.0.0 is not published to PyPI. `uv.lock` and `pyproject.toml` pin the
environment, including FastMCP **4.0.2** and `pydantic-monty` **0.0.18**.
Do not install an LLM provider extra for compilation: LLM enhancement is retired
and explicitly rejected in v2.

Tests need no API keys or real `.env`. To try the example from the checkout root:

```bash
cp .env.example .env
cp config/swaggers.yaml.example config/swaggers.yaml
uv run --frozen --extra saas gryphon compile
uv run --frozen --extra saas python examples/demo.py
```

The demo uses a real in-process MCP client, sums explicit inputs, and reuses its
recipe offline. Compilation only adds discovery from the local weather spec;
the demo does not call Open-Meteo or a model. Stop other processes using the same
run database before running it.

## Development workflow

1. Create a focused branch from the target branch; use `feat/`, `fix/`, `docs/`,
   `test/`, `refactor/`, or `chore/` prefixes.
2. Read affected files in full, then make the smallest complete change. Respect
   existing local changes and other contributors' work.
3. Add positive, negative, and cleanup-path tests. Do not weaken policy to make
   fixtures pass.
4. Update relevant documentation in the same change. Review README for every
   code change, even when no public wording ultimately needs editing.
5. Run the complete quality suite and include actual results in your PR.

```bash
uv run --frozen --extra saas ruff check src/ tests/
uv run --frozen --extra saas ruff format --check src/ tests/
uv run --frozen --extra saas mypy --strict src/ tests/
uv run --frozen --extra saas pytest --cov-fail-under=90
uv run --frozen --extra saas pre-commit run --all-files
```

**90% coverage is the hard floor; 100% is the target.** The local
`pytest-coverage` hook invokes locked uv commands and enforces the same floor.
Mypy checks both `src` and `tests`. Hooks may apply Ruff fixes; review those
changes and rerun checks. Do not bypass hooks, lower coverage, exclude difficult
modules, delete tests, or suppress errors to get a green result.

For focused iteration, run the relevant test file. Focused checks do not replace
the full gate:

```bash
uv run --frozen --extra saas pytest tests/unit/test_server.py
uv run --frozen --extra saas pytest tests/integration/test_protocol.py
```

Compiler changes also need a dry run against safe fixture/example configuration:

```bash
uv run --frozen --extra saas gryphon compile --dry-run
```

`--dry-run` does not write output but can fetch a configured remote spec. Use
local fixtures when an offline check is required.

## Test boundaries

- **Normal suite:** no live upstream API, credentials, model, or Docker daemon.
  Use `_env_file=None`, `tmp_path`, isolated databases, and fake credentials.
- **Unit tests:** mock outbound HTTP/DNS and Docker; use `respx` or an injected
  transport where appropriate. Async Docker calls need async-aware mocks.
- **Integration tests:** exercise real compilation, the restricted VM, storage,
  and MCP sessions. Protocol tests use real modern and legacy clients and a
  loopback HTTP server, including authentication failures and structured output.
- **Fixtures:** prefer shared setup in `tests/conftest.py` and OpenAPI examples in
  `tests/fixtures/`. Never depend on an operator's configured APIs or stores.
- **Security:** every changed guard needs both denial and allowed-case coverage.
  Exercise malformed schemas, owner isolation, DNS rebinding, write permits,
  cancellation, drift, bounded output, crash receipts, and safe cleanup.

### Optional PostgreSQL control-plane tests

```bash
GRYPHON_TEST_POSTGRES=1 uv run --frozen --extra saas pytest tests/integration/test_saas_postgres.py
```

Read [`test_saas_postgres.py`](tests/integration/test_saas_postgres.py) first.
It creates and removes its own disposable **postgres:17.6** Docker container,
using temporary storage, a generated password, and a random loopback port.
It may pull the pinned image. Never substitute an operator database URL or run
Compose against real operator configuration for verification. Coverage includes
tenant isolation, immutable specs, hashed-key lifecycle, quotas/aggregate usage,
and refusal of a second hosted worker. Normal tests use isolated temporary
SQLite control stores or fakes; they need no live PostgreSQL.

Hosted review must preserve independent administrator sessions/CSRF and channel
keys, read-only uploaded catalogs, disabled host-auth inheritance, and per-channel
local execution state. PostgreSQL stores control metadata only. Docker imports
may only narrow preinstalled approved libraries; arbitrary installs are forbidden.

### Optional live Docker smoke tests

Build the image and opt in explicitly:

```bash
docker build -t gryphon-sandbox:2.0.0 sandbox/
GRYPHON_TEST_DOCKER=1 uv run --frozen --extra saas pytest tests/integration/test_execution_docker.py
```

Read [`test_execution_docker.py`](tests/integration/test_execution_docker.py)
before running it. The fixture deliberately selects `runc` for benign offline
transport/library tests. This is **not** a production runtime recommendation or
a verification of gVisor isolation. The deployed Docker profile defaults to
`runsc` and refuses missing runtimes without falling back. Do not change that
security default to accommodate a development machine.

The tests use temporary stores, invoke no external API, and clean only their own
containers. Provision/start Docker yourself; Gryphon never starts the daemon.
The optional compute profile is different from the restricted Compose service,
which deliberately has no Docker socket mount.

## Coding and design standards

- Python 3.13+, future annotations, fully typed signatures, modern union and
  built-in collection types. Strict typing applies to tests too.
- Google-style public docstrings; concise docstrings for private helpers.
  Use named constants, immutable defaults, and validated boundary models.
- Keep files within 400 lines and functions within 50 lines. Shared domain
  models belong in `src/gryphon/models/__init__.py`; custom exceptions belong in
  `src/gryphon/errors.py`. Prefer existing files; new files need a task rationale.
- Use injected `GryphonConfig`/dependencies instead of scattered environment
  access. HTTP goes through the broker network layer, not a bypass client.
- Use structured safe logging to stderr; no `print()` in server/runtime code,
  no raw traces or input/credential values in errors. MCP adapters return stable
  domain-error envelopes while the SDK handles invalid protocol/schema input.
- Keep async code responsive and cleanup cancellation-safe. Never orphan workers
  or delete containers/files not owned by the current operation.
- Preserve the two-argument restricted capability contract:
  `call_tool("server.function", arguments)`. Inputs and replay parameters are JSON
  objects, never source substitutions. `main()` is never auto-called.
- Credentials stay in the host broker. Generated modules must not execute on
  the host; optional Docker remains offline and credential-free.
- Writes require exact administrator permits in addition to the global switch
  and source policy. Tool hints, guides, idempotency keys, and model claims do not
  authorize actions. Keep native MCP Tasks disabled until actually implemented.

## Pull requests and commits

Use [Conventional Commits](https://www.conventionalcommits.org/) with an
imperative summary of at most 72 characters, for example:

```text
fix(broker): reject cross-origin credential delegation
test(runtime): cover interrupted run ownership
docs(readme): clarify structured recipe inputs
```

Keep one logical change per commit and one focused purpose per PR. Use `git`
commands for repository operations. Explain the problem, solution, security
impact, compatibility changes, and validation results; avoid invented test
counts, speedups, release promises, or unverified deployment claims.

Before requesting review:

- [ ] Public behavior, examples, CLI flags, and env names match the implementation.
- [ ] Full Ruff checks, formatting check, strict mypy, tests, and hooks pass.
- [ ] Coverage is at least 90%; new behavior and failure paths are tested.
- [ ] README is reviewed; SECURITY and CHANGELOG are updated when relevant.
- [ ] No real credentials, private config, `.env`, runtime data, or unrelated files.
- [ ] Default isolation, write policy, output limits, and owner checks are preserved.
- [ ] Resource lifecycle, restart behavior, and cancellation consequences are documented.
- [ ] Any omitted optional infrastructure check is named explicitly, not called a pass.

## Bugs, features, and security reports

For ordinary bugs, open an
[issue](https://github.com/hypen-code/gryphon/issues) with:

- `uv run --frozen --extra saas gryphon --version`, Python/OS, and relevant installed versions;
- execution profile, transport, and Docker/runtime details if applicable;
- a minimal sanitized reproduction and expected versus actual behavior;
- relevant safe diagnostics from `gryphon doctor` and the exact failing command.

`doctor` is read-only and does not verify daemon health or upstream access.
Review diagnostics before sharing: paths and local setup can still be private.
Never post `.env`, real tokens, API payloads containing personal data, or raw
credential-bearing logs.

For feature proposals, explain the user problem, alternatives, proposed scope,
and trust-boundary changes. Check [ROADMAP.md](ROADMAP.md) first; it records
unimplemented directions, not scheduled commitments.

**Report vulnerabilities privately**, following [SECURITY.md](SECURITY.md), not
through public issues. Include a minimal proof of concept using fake credentials
and synthetic data. Do not probe a deployment you are not authorized to test.
