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
No PyPI publication is claimed here. `uv.lock` and `pyproject.toml` pin the
environment, including FastMCP **4.0.2** and `pydantic-monty` **0.0.18**.
Do not install an LLM provider extra for compilation: LLM enhancement is retired
and explicitly rejected in v2.

Tests need no API keys or real `.env`. For the least-setup stdio and SQLite SaaS
launch commands, use [README.md](README.md); developer extras are not required for
stdio. Do not copy over an existing `.env`, compile into operator output, or run
migrations against an operator database during development.

The real in-process MCP demo sums explicit inputs and reuses its recipe offline.
Compilation only adds weather discovery; the demo calls neither Open-Meteo nor a
model. Exercise it safely through the isolated lifecycle tests:

```bash
uv run --frozen --extra saas pytest tests/unit/test_cli_lifecycle.py
```

Use temporary configuration/state when running `examples/demo.py` manually;
legacy commands discover ambient dotenv. Stop any competing process first.

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

Hosted review must preserve independent browser sessions/CSRF and channel keys,
read-only catalogs, disabled host-auth inheritance and per-channel local state.
PostgreSQL stores control metadata only. Docker imports may only narrow approved
preinstalled libraries; arbitrary installs are forbidden.

Account changes need positive and negative tests for `platform_admin` authority,
immutable single-tenant membership, foreign-tenant denial on every API surface,
user-list denial for tenant users, password changes/resets, and revision-based
session invalidation across disable/re-enable. Include additive SQLite upgrade
preservation, salted password hashes, bounded hashing admission and cancellation.
Use temporary databases only; never run schema upgrades against operator data.
Independently issued channel keys must remain independent from account sessions;
offboarding requires deliberate rotation/revocation of exposed keys.

### Analytics regression checks

Focused analytics iteration (not a substitute for the full **90% coverage gate**):

```bash
uv run --frozen --extra saas pytest tests/unit/test_execution_metrics.py tests/unit/test_broker_metrics.py tests/unit/test_saas_analytics.py tests/unit/test_analytics_ui.py
uv run --frozen --extra saas pytest tests/unit/test_saas_traffic.py tests/unit/test_saas_analytics_reports.py tests/integration/test_saas_analytics_http.py
GRYPHON_TEST_POSTGRES=1 uv run --frozen --extra saas pytest tests/integration/test_saas_analytics_postgres.py
```

Read the PostgreSQL fixture before opting in; it uses disposable databases, never
operator data. Keep positive/negative tenant and channel scope tests, additive
upgrades, retention and the 100,000-per-tenant receipt cap, including prevention of
cross-tenant eviction of in-retention receipts. Cover no-backfill empty windows,
incomplete traffic, terminal background completion, replay and idempotent duplicates.
Verify observer failure/cancellation remains nonfatal and content-free. Test
once-per-request observation before final ASGI handoff, incomplete SDK fallback,
independent two-second writer timeouts and fixed allowlisted-name error counting.
SDK-produced response bytes do not prove client reception/model consumption;
response-production timing excludes its own persistence and is not client end-to-end latency.

Analytics review must separate measured canonical bytes/body wire bytes/backend
starts from heuristic token equivalents. Pair only successful eligible API-backed
runs against **full** final JSON (including artifacts); permit negative expansion
and null/N/A without a baseline. Broker attempts are not HTTP dispatches; zero
accepted responses does not establish pure compute. Static source lines and
reused source bytes are not instructions executed or model generation avoided.
Wire traffic excludes initialization, `tools/list`, headers and agent context;
only structured-payload bytes remove duplicate text representations. Histogram
p50/p95 are upper bounds, not exact; wall timing is not CPU or time saved.
Actual model context/generation/reasoning/billing must remain unobservable, not
inferred from byte/4 or claimed as dollar/time/round-trip savings. Marketing
examples must identify the UTC window, paired population, denominator and
methodology; use report values, not invented numbers. Operational run success and
payload reduction do not measure answer correctness or equivalent task quality.
Top-level array item totals are not semantic records; broad cache-error counts do
not distinguish exact misses from storage failures. Preserve methodology and
explicit window tenant/channel IDs in downloads; protect activity metadata.

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
  models belong in `src/gryphon/models/` with public exports in `__init__.py`;
  custom exceptions belong in `src/gryphon/errors.py`. New files need a task rationale.
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

## Release to PyPI through GitHub

The distribution is **`gryphon-runtime`**, the CLI/import package is **`gryphon`**.
The checked-in workflow is [`.github/workflows/publish.yml`](.github/workflows/publish.yml).
Its existence does not mean any version has been published. No API token is
needed in the repository: publication uses PyPI **Trusted Publishing (OIDC)**.

### One-time maintainer setup

1. In PyPI, configure a **pending publisher** for the new project (or add a
   trusted publisher to the existing project you control), with these exact values:

   | PyPI field | Value |
   |---|---|
   | Project name | `gryphon-runtime` |
   | GitHub owner | `hypen-code` |
   | Repository | `gryphon` |
   | Workflow filename | `publish.yml` |
   | Environment | `pypi` |

2. In GitHub repository settings, create environment **`pypi`**, require a trusted
   reviewer, prevent self-review where available, and restrict deployment to
   reviewed version tags. Protect release tags from unreviewed creation/movement.
   Environment protection is a maintainer setting, not installed by the workflow.
3. Ensure Actions may run the workflow and obtain an OIDC token in the protected
   publish job. Do not create/commit a PyPI token, `.env`, private config or credentials.

### Each release

1. Review the complete diff and CHANGELOG. Keep `pyproject.toml`'s version,
   `src/gryphon/__init__.py`'s `__version__`, and the tag identical: for version
   **2.0.0**, the tag must be **`v2.0.0`**. Never move/reuse a published version tag.
2. Run all local quality gates above. On the reviewed commit, a maintainer may
   explicitly create a tag with `git tag -a v2.0.0 -m "Gryphon 2.0.0"`, review it,
   and deliberately push with `git push origin v2.0.0`. **Agents must not commit,
   tag, push or publish automatically**; these are separate maintainer actions.
3. In GitHub, publish a non-draft, non-prerelease **Release** for that tag.
   Alternatively dispatch `publish.yml` explicitly **at the version tag**, not
   at a branch. A branch push or tag push alone does not publish to PyPI.
4. The build job checks tag/checkout/source-version agreement, installs the locked
   dev+SaaS environment, runs Ruff lint/format, strict mypy and the full pytest
   suite with **90% minimum coverage**, then builds sdist/wheel. It checks metadata,
   CLI entry points and packaged assets and runs an installed-wheel real stdio
   MCP smoke test outside the checkout, without secrets or ambient dotenv.
5. Review the successful gates/artifact and approve the protected **`pypi`** job.
   Only this separate job receives `id-token: write`; it downloads the checked
   artifacts and publishes them with OIDC, without checking out/running the package.
6. Verify the actual PyPI version and installation before announcing availability.
   If a gate fails, fix/review it rather than bypassing it. PyPI files cannot be
   overwritten; a faulty published package requires a new version.

Before publication, install from the checkout or a locally built wheel. Only
**after the real PyPI release** may users run
`uvx --from gryphon-runtime==2.0.0 gryphon stdio` or
`python -m pip install gryphon-runtime==2.0.0` (hosted extra:
`'gryphon-runtime[saas]==2.0.0'`). Follow README for source configuration; package
installation alone does not configure API authority or provision a hosted service.

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
