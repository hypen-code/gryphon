# Gryphon

> **APIs were designed for developers. Gryphon makes them usable by AI agents.**

[![CI](https://github.com/hypen-code/gryphon/actions/workflows/ci.yml/badge.svg)](https://github.com/hypen-code/gryphon/actions)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Named for the legendary guardian, **Gryphon** sits between an agent's code and
its API authority. It compiles OpenAPI catalogs, lets agents discover only the
operations they need, and runs bounded Python through a credential-holding
broker—not through unrestricted host execution.

The original idea remains: **a small set of meta-tools, not one MCP tool per API
endpoint**. Discover → inspect → execute → reuse. Compose calls and reduce data
in code before returning it to the model.

**Version 2.0.0** · Python **3.13+** · FastMCP **4.0.2** ·
`pydantic-monty` **0.0.18**. Real-client conformance tests cover MCP
**2026-07-28** negotiation and legacy initialization. Native MCP Tasks are not
implemented or advertised; background runs use portable application handles.

Gryphon is a **local/operator deployment**, not a completed multi-tenant SaaS.
The distribution name is `gryphon-runtime`; the import package and CLI are
`gryphon`. This version is **not published to PyPI**: install from this checkout.

## Quick start

Prerequisites: Git, [uv](https://docs.astral.sh/uv/), and Python 3.13+.
Use Linux/macOS, or the container deployment on Windows; persistent-store locks
require POSIX support. The default restricted profile needs **no Docker, model,
model API key, or upstream credentials** for the included offline demo.

Run these commands from the repository root:

```bash
git clone https://github.com/hypen-code/gryphon.git
cd gryphon
uv sync --frozen --extra dev
cp .env.example .env
cp config/swaggers.yaml.example config/swaggers.yaml
uv run --frozen gryphon compile
uv run --frozen python examples/demo.py
```

The config example uses [`examples/weather.yaml`](examples/weather.yaml), a
local OpenAPI description of the public Open-Meteo forecast API. Compilation
does not call the forecast endpoint. The demo uses a **real in-process MCP
client**, computes a sum offline, then reuses the same recipe with new inputs.
Compilation is optional for that computation; it adds weather discovery.

### Connect an MCP client over stdio

`uv run --frozen gryphon compile` prints MCP client JSON to **stdout**, even
when all sources are already up to date. Logs stay on **stderr**. Copy its
`mcpServers` entry into your client's configuration; no client settings are
changed automatically. Dry runs, failed/empty compilations, and compilation
inside `serve`/`run` do not print client JSON.

The generated entry uses absolute storage paths, disables startup recompilation,
and references the selected env-file path without copying its credentials.
Keep operator settings/credentials in that private file or the launch environment.
For this manual equivalent, replace `/absolute/path/to/gryphon` with
your actual checkout path; clients may not expand `~`.

```json
{
  "mcpServers": {
    "gryphon": {
      "command": "/absolute/path/to/gryphon/.venv/bin/gryphon",
      "args": ["serve", "--env-file", "/absolute/path/to/gryphon/.env"],
      "env": {
        "GRYPHON_COMPILE_ON_STARTUP": "false",
        "GRYPHON_COMPILED_OUTPUT_DIR": "/absolute/path/to/gryphon/compiled",
        "GRYPHON_SWAGGER_CONFIG_FILE": "/absolute/path/to/gryphon/config/swaggers.yaml",
        "GRYPHON_CACHE_DB_PATH": "/absolute/path/to/gryphon/data/cache.db",
        "GRYPHON_RUN_DB_PATH": "/absolute/path/to/gryphon/data/runs.db",
        "GRYPHON_ARTIFACT_DIR": "/absolute/path/to/gryphon/data/artifacts"
      }
    }
  }
}
```

Stdio trusts the local operator. Run only one Gryphon process per run database:
stop the demo or existing server before starting another against the same paths.
Logs go to stderr so stdout remains available for the MCP protocol.

## The MCP interface

| Core tool | Purpose |
|---|---|
| `list_servers` | Compact, paginated API summaries |
| `search_functions` | Find relevant capabilities without loading the whole catalog |
| `get_functions` | Inspect schemas and invocation metadata for 1–5 functions |
| `execute_code` | Execute code with structured `inputs` and optional `input_schema` |
| `run_cached_code` | Reuse unchanged source with a complete new `params` object |
| `submit_code` | Submit work and receive a persistent run receipt |
| `get_run` | Poll a caller-owned run |
| `cancel_run` | Revoke an active run and wait for its worker to stop |
| `list_recipes` | Search the caller's cached recipe summaries |
| `read_artifact` | Read caller-owned JSON output in bounded chunks |

The `reusable_code_guide` prompt explains the execution contract on demand.
With `GRYPHON_ENABLE_ADDITIONAL_TOOLS=true`, `list_skills` and
`get_server_skills` expose optional server guides. Guides are **untrusted data**,
not embedded in initialization instructions and never write approval.

Discovery responses include a registry fingerprint and truncation metadata.
Follow `next_cursor` where provided; narrow searches when truncated. MCP
`readOnlyHint` annotations are client hints, **not authorization**.

### Call an API, then reuse the program

First search for weather, then call `get_functions` with:

```json
{"functions": [{"server_name": "weather", "function_name": "get_forecast"}]}
```

Use the inspected schema, not guessed parameter names. This `execute_code`
payload calls the public API, unlike the offline demo:

```json
{
  "code": "result = await call_tool(\"weather.get_forecast\", {\"latitude\": inputs[\"latitude\"], \"longitude\": inputs[\"longitude\"], \"current\": \"temperature_2m\"})",
  "description": "current weather by coordinates",
  "inputs": {"latitude": 51.5074, "longitude": -0.1278},
  "input_schema": {
    "type": "object",
    "properties": {
      "latitude": {"type": "number", "minimum": -90, "maximum": 90},
      "longitude": {"type": "number", "minimum": -180, "maximum": 180}
    },
    "required": ["latitude", "longitude"],
    "additionalProperties": false
  }
}
```

Inside restricted Python, the only external capability has **two arguments**:

```python
result = await call_tool("weather.get_forecast", {"latitude": inputs["latitude"], "longitude": inputs["longitude"], "current": "temperature_2m"})
```

Pass the returned `cache_id` to `run_cached_code`:

```json
{"cache_id": "<returned-cache-id>", "params": {"latitude": 48.8566, "longitude": 2.3522}}
```

- `inputs` and `params` are JSON objects. `params` replaces the **entire** input
  object; it is not a patch and does not recover previous input values.
- Assign `result` to a JSON-native value. No `main()` is required; if you define
  one, call it explicitly. Gryphon never auto-executes it.
- Restricted Python supports no imports, filesystem, direct network, or host
  environment access. It is a subset, not a drop-in CPython environment.
- `input_schema` is bounded. References, regex constraints, and combinators such
  as `$ref`, `pattern`, `allOf`, `anyOf`, and `oneOf` are rejected.
- Recipe identity includes owner, exact source, schema, and catalog/policy
  digest. Replay repeats validation and authorization; catalog or policy drift
  rejects stale recipes. Reordering or repeating the same exact write permits
  does not change policy identity; adding or removing a permit does.
  **Input values are never rewritten into source.**
- A recipe caches code, not an API response. Reuse can call the API again.

### Results, background runs, and cancellation

Execution returns native structured MCP data with `success`, result `data`,
handles when available, and stable `error_type` categories on failure. Docker
backend unavailability retains `sandbox_unavailable` in tool results and saved
receipts. Raw prints, upstream error bodies, and exception traces are omitted.
A print-byte summary may be returned; do not use printing to deliver results.

Large successful results within the full-result limit become owner-scoped JSON
artifacts. Use `read_artifact` with the returned `artifact_id`, then follow
`next_offset` until `eof`. Requested chunks are at most **8192 bytes** and may be
smaller to fit the context budget. Artifacts do not bypass the full-result cap.

`submit_code` accepts the same code/input contract as `execute_code`, returns an
`id`, and can be polled with `get_run(run_id=...)`. Receipt states are `queued`,
`running`, `succeeded`, `failed`, `cancelled`, and `interrupted`.

Optional `idempotency_key` values deduplicate matching requests within an owner
while the receipt is retained; conflicting requests fail. This is **not
exactly-once delivery**. On restart, persisted queued/running receipts become
`interrupted`; code and writes are **not automatically replayed or resumed**.
Check upstream state before deliberately retrying an interrupted write.

Cancellation revokes broker authority and waits for worker cleanup. It cannot
undo an API action already accepted upstream. Receipt and artifact retention
are bounded; they are not a permanent audit archive or durable workflow engine.

## Configure APIs and policy

The public example is deliberately small:

```yaml
servers:
  - name: weather
    swagger_url: ./examples/weather.yaml
    is_read_only: true
```

`swagger_url` accepts local files or policy-approved HTTP(S) documents. Relative
paths use the compilation working directory. `base_url` overrides the spec's URL.
OpenAPI **3.0/3.1** and Swagger **2.0** produce v2 manifests with native parameter,
JSON-body, and supported response schemas; OpenAPI 3.2 is not yet supported.
Unsupported request semantics fail closed. Unsupported response schemas are
reported and omitted, never presented as a validation guarantee. Supported
output schemas are validated by the broker before results enter the sandbox.
Optional query/header `null` means omission. Required/path null values and null
query-array elements are rejected; JSON-body null is supported when declared.

Generated Python is documentation/SDK output, **never imported or executed by
the MCP host**. Legacy `top_level_functions` selections do not promote direct
MCP tools. `--llm-enhance` and `GRYPHON_LLM_ENHANCE=true` are explicitly rejected
in v2: compilation is deterministic and needs no model key.

### Credentials and writes

Public APIs need no auth block. For authenticated APIs, put environment
references in trusted configuration, for example:

```yaml
auth:
  type: static
  value: "Bearer ${UPSTREAM_API_TOKEN}"
```

Supported host-side auth types are `static`, `jwt`, `basic`, OAuth2 client
credentials (`oauth2`), `keycloak`, and `session`. Dynamic tokens are fetched and
cached by the broker. Set secrets in the operator environment or a private env
file—not in code, tool inputs, committed YAML, or client configuration snippets.
`GRYPHON_{SERVER}_AUTH` and JSON `GRYPHON_{SERVER}_EXTRA_HEADERS` are host-side
options. Credentials are **never injected into either execution sandbox**.

`is_read_only: true` excludes write methods during compilation and is checked
again by the broker. To permit a write, its source must allow writes, and the
administrator must set **both** of these settings with exact inspected names
(the operation below is illustrative):

```dotenv
GRYPHON_ALLOW_WRITES=true
GRYPHON_ALLOWED_WRITE_OPERATIONS=["orders.create_order"]
```

These are administrator permits, not an LLM approval flag. There is no
interactive approval UI. API descriptions, guides, user code, and tool hints
cannot change policy.

### Important settings

See [`.env.example`](.env.example) and [`GryphonConfig`](src/gryphon/config.py)
for the complete settings. Explicit environment variables override the env file.
The default env file is `.env` in the working directory, with checkout-root
fallback; use `--env-file` for an unambiguous location.

| Variable | Default | Meaning |
|---|---|---|
| `GRYPHON_HOST` / `GRYPHON_PORT` | `127.0.0.1` / `8000` | HTTP listener |
| `GRYPHON_HTTP_AUTH_TOKEN` | unset | Required for HTTP; at least 32 characters |
| `GRYPHON_COMPILE_ON_STARTUP` | `true` | `serve` compiles before startup |
| `GRYPHON_SANDBOX_MODE` | `restricted` | Restricted API execution or offline `docker` |
| `GRYPHON_CONTEXT_BUDGET_BYTES` | `16384` | Discovery response budget |
| `GRYPHON_DISCOVERY_LIMIT` | `10` | Maximum items returned per discovery page |
| `GRYPHON_MAX_CODE_SIZE_BYTES` | `65536` | Source byte limit |
| `GRYPHON_MAX_OUTPUT_SIZE_BYTES` | `65536` | Inline output/print budget |
| `GRYPHON_MAX_RESPONSE_SIZE_BYTES` | `2097152` | Full JSON/upstream response limit |
| `GRYPHON_EXECUTION_TIMEOUT_SECONDS` | `30` | Deadline; restricted VM has a 30-second hard ceiling |
| `GRYPHON_MAX_CONCURRENT_EXECUTIONS` | `4` | Active execution limit |
| `GRYPHON_QUEUE_TIMEOUT_SECONDS` | `5` | Bounded admission wait |
| `GRYPHON_MAX_TOOL_CALLS` | `50` | Per-run capability budget |
| `GRYPHON_SANDBOX_MEMORY_BYTES` | `64000000` | Restricted VM memory budget |
| `GRYPHON_ALLOWED_DOMAINS` | `[]` | Additional exact-host restriction; JSON array |
| `GRYPHON_ALLOW_PRIVATE_NETWORKS` | `false` | Explicit private/loopback API opt-in |
| `GRYPHON_ALLOW_WRITES` / `GRYPHON_ALLOWED_WRITE_OPERATIONS` | `false` / `[]` | Two-part write policy |
| `GRYPHON_CACHE_TTL_SECONDS` / `GRYPHON_CACHE_MAX_ENTRIES` | `3600` / `500` | Recipe retention |
| `GRYPHON_RUN_TTL_SECONDS` / `GRYPHON_RUN_MAX_ENTRIES` | `86400` / `1000` | Receipt retention |
| `GRYPHON_ARTIFACT_MAX_ENTRIES` | `100` | Retained artifacts per owner |

Public API, authentication, and document destinations **require HTTPS**.
`GRYPHON_ALLOWED_DOMAINS` additionally restricts exact hostnames; use a JSON array,
not wildcards or CSV. HTTP is allowed only with private-network opt-in and solely
approved private/loopback DNS answers. Metadata, link-local, reserved, and mixed
public/private destinations remain denied. Clients use DNS pinning, origin-scoped
pools, verified TLS, no environment proxies, and no redirects. See [SECURITY.md](SECURITY.md).

## Operate Gryphon

```bash
uv run --frozen gryphon --version
uv run --frozen gryphon doctor
uv run --frozen gryphon compile --dry-run
uv run --frozen gryphon serve
uv run --frozen gryphon run
uv run --frozen gryphon --env-file /absolute/path/to/operator.env doctor
uv run --frozen gryphon doctor --env-file /absolute/path/to/operator.env
```

`serve` respects `GRYPHON_COMPILE_ON_STARTUP`; `run` compiles **once**, then serves.
`doctor` reports read-only JSON diagnostics: it does not compile, open runtime
stores, call APIs, or probe/start Docker. `--env-file` works before or after the
subcommand.

**Stop the server before cleaning.** `gryphon clean --yes` reversibly renames
only recognized compiled output and a closed recipe cache to adjacent
`.gryphon-archive-...` paths. `clean --yes --dry-run` validates without moving;
`clean compile --yes` archives then compiles. Unknown files, unsafe paths, links,
and active SQLite sidecars are refused. Runs, artifacts, and config are retained.
Restore manually while stopped by renaming an archive to its original path,
without overwriting newer data. This is not an arbitrary-directory delete tool.

### Authenticated HTTP

```bash
export GRYPHON_HTTP_AUTH_TOKEN="$(uv run --frozen python -c 'import secrets; print(secrets.token_urlsafe(48))')"
uv run --frozen gryphon serve --transport http
```

Connect to `http://127.0.0.1:8000/mcp` with an MCP client using
`Authorization: Bearer <your-token>`. HTTP startup refuses missing/short tokens;
settings use `SecretStr` and the verified token maps to a fixed `operator`
identity, distinct from stdio's `local` owner. Sharing a token shares that
identity. Use a **TLS reverse proxy before public exposure**; bearer auth alone
does not encrypt traffic or provide multi-tenant identity management.

### Container service

After the quick-start config copies, set the random token above, then:

```bash
docker compose up --build -d
```

The root image uses the lockfile and runs as UID 1000. Compose uses restricted
execution, read-only configuration, named volumes for compiled/data storage,
resource limits, loopback port publishing, and **no Docker socket mount**. It
requires `.env` and the auth token. Its health check is TCP connectivity only,
not authenticated MCP readiness; there is no `/health` endpoint.

### Optional offline full Python

Use Docker only when computation needs CPython and the libraries in
[`sandbox/requirements.txt`](sandbox/requirements.txt). Provision the Docker
daemon and **gVisor `runsc`** yourself, then build the tagged image:

```bash
docker build -t gryphon-sandbox:2.0.0 sandbox/
GRYPHON_SANDBOX_MODE=docker uv run --frozen gryphon serve
```

Missing daemon, image, or configured runtime fails closed—no automatic startup
or weaker-runtime fallback. Each execution gets a fresh container: UID 1000,
all capabilities dropped, 64-PID limit, 256 MiB RAM/swap cap, half-core CPU quota,
read-only root, bounded `/tmp` tmpfs, and no host mounts or network. Docker gets
only code and explicit inputs, **no credentials and no `call_tool`/API access**.
AST restrictions still apply; full Python does not mean unrestricted host access.
This profile is separate from the default Compose service.

## Measure local execution

```bash
uv run --frozen python examples/benchmark.py --iterations 25 --concurrency 1
uv run --frozen python examples/benchmark.py --iterations 100 --concurrency 4
```

This repeatable offline benchmark verifies each result and reports startup,
median/p95 latency, and throughput including durable receipts. It performs no
model or upstream API calls; these timings are not end-to-end agent benchmarks.

## Development and support

```bash
uv run --frozen ruff check src/ tests/
uv run --frozen ruff format --check src/ tests/
uv run --frozen mypy --strict src/ tests/
uv run --frozen pytest --cov-fail-under=90
uv run --frozen pre-commit install
uv run --frozen pre-commit run --all-files
```

The **90% coverage floor** remains mandatory; the target is **100%**. The normal
suite needs no live upstream API or Docker. Optional Docker checks and developer
rules are in [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

See [CHANGELOG.md](CHANGELOG.md) for v2 changes and [ROADMAP.md](ROADMAP.md) for
work that is not implemented. Report bugs through the
[issue tracker](https://github.com/hypen-code/gryphon/issues);
report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

MIT licensed; see [LICENSE](LICENSE).
