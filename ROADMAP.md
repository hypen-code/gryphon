# Gryphon Roadmap

This document covers **work not implemented in Gryphon 2.0.0**. It is a set of
possible next directions, not a delivery schedule or a claim of readiness.
Current behavior belongs in [README.md](README.md); released code changes belong
in [CHANGELOG.md](CHANGELOG.md).

The guiding purpose remains an API-agent backend: discover a bounded catalog,
compose governed API calls in code, and reuse programs with structured inputs.
Any extension must preserve that model and the [security boundaries](SECURITY.md).

## Hosted service expansion

Admin-managed tenants, immutable JSON/YAML uploads, channel bindings and keys,
a browser UI, per-channel runtime isolation, aggregate usage and a PostgreSQL
control plane are implemented, as are named platform-admin and single-tenant-user
accounts with password lifecycle management; see README. Remaining work:

- Self-service signup/invitations, SSO/identity federation, custom granular roles,
  multi-tenant user membership, billing and subscription lifecycle management.
  These are separate from the fixed platform-admin/single-tenant-user account model.
- Tenant upstream credential storage/rotation with a reviewed secret-management
  boundary. Current channels are public-API-only and cannot inherit host auth.
- Multi-worker ownership, distributed admission, coordinated recovery, shared
  execution storage, and HA deployment. The current database lease permits
  exactly one active hosted worker; PostgreSQL does not store execution data.
- Automated coordinated backups/restores, TLS provisioning, external operational
  alerting, public-service abuse controls and tested incident response procedures beyond
  the implemented bounded request/rate limits.
- Independent tenant-isolation assessment, penetration testing, and formal
  security assurance; implemented controls do not imply certification.

## Stronger execution backends and independent verification

Potential work includes independently assessed process/VM isolation backends,
reproducible escape testing, and audited deployment profiles. Any additional
backend must preserve credential exclusion, resource bounds, revocable
capabilities, and fail-closed selection without a weaker automatic fallback.

The existing restricted VM and optional offline Docker profile are implemented;
stronger assurance, independent audits, and isolation certification are not.

## Reproducible measurements and operational visibility

A published benchmark suite could measure end-to-end latency, context/output
bytes, admission under load, memory use, and recipe reuse on disclosed hardware
and pinned dependencies. It should compare representative API workloads, validate
answer correctness/equivalent task quality, and report distributions and failures
rather than single best-case numbers. Operational run success and payload reduction
alone do not establish that a user's task was correctly answered.

Tenant analytics now provide scoped 1–90-day UTC reports, a 7/30/90-day UI and
filtered JSON downloads with methodology. They measure canonical payload bytes,
recognized tool-request traffic, terminal outcomes and backend replay starts;
paired successful comparisons include full final artifact content. These features
are implemented, not roadmap promises; see README for limits and privacy.

Remaining measurement work includes independently reproducible comparisons,
reviewed external telemetry integrations and operator alerting. Actual model
context, generation, reasoning and billing would require separately authorized
client/provider evidence: Gryphon's byte/4 heuristic cannot infer them. Reused
source is executed again, not proven generation avoided. No CPU, elapsed-time,
dollar or round-trip savings follow from the present counters. Crash-reconciled
analytics would require further design; bounded best-effort observers and retained
deduplication digests are not a complete billing audit or exactly-once delivery.

## Durable workflow integration

Possible integrations could add explicit workflow checkpoints, operator-driven
reconciliation, upstream idempotency/compensation strategies, and externally
managed workflow engines. Such work needs defined behavior when an API accepts
an action but the local result is lost.

Current run receipts do **not** resume code after a crash or promise exactly-once
effects. Native MCP Tasks are also not implemented or advertised; any future
Tasks integration needs actual protocol support and conformance tests, not a
rename of the existing `submit_code`/`get_run`/`cancel_run` application handles.

## Proposing work

Open a [proposal](https://github.com/hypen-code/gryphon/issues) with the
user problem, intended contract, security implications, alternatives, and a
verifiable acceptance plan. New work must retain the mandatory **90% coverage
floor**, target **100%**, and document limitations rather than weaken controls.
