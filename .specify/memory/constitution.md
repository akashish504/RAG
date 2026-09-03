<!--
Sync Impact Report
Version change: (none, initial) → 1.0.0
Modified principles: n/a (initial ratification)
Added sections: Core Principles (I–V), Security & Secrets Requirements,
  Development Workflow, Governance
Removed sections: none
Templates requiring updates:
  - .specify/templates/plan-template.md ✅ (generic "[Gates determined based
    on constitution file]" Constitution Check section already compatible,
    no textual reference to sync)
  - .specify/templates/spec-template.md ✅ (no constitution references)
  - .specify/templates/tasks-template.md ✅ (no constitution references)
Follow-up TODOs: none — no placeholders deferred
-->

# Dalberg MCP Pipeline Constitution

## Core Principles

### I. Source-Agnostic Retrieval

All retrieval functionality MUST be exposed through the source registry
(`retrieval/config`), not hardcoded to a single backend. New logical sources
(Airtable bases, OpenSearch indices, etc.) MUST be addable via
`config/retrieval_sources.yaml` and become visible through
`list_sources`/`get_schema` without code changes to the MCP tool layer.
Rationale: the MCP server's value is querying multiple heterogeneous sources
through one consistent tool surface (`retrieval/mcp/server.py`);
source-specific logic in the tool layer defeats that and creates N one-off
integrations instead of one registry.

### II. Reuse Before Building

Before adding a new helper, client wrapper, or token/auth pattern, search the
codebase for an existing one and extend or reuse it (e.g. `s3_client()` /
`secrets_client()` in `pipeline/common/aws.py` for AWS access,
`retrieval/citation_token.py`'s signed-token pattern for any future
opaque/signed token need). A new abstraction is only justified when no
existing pattern fits after this search. Rationale: this codebase already
carries overlapping mechanisms from past iterations (a removed v1 MCP mount,
in-progress secrets migration); unreviewed duplication is how that
accumulates and is a recurring source of confusion and drift.

### III. No Endpoint Ships Without an Explicit Auth Decision (NON-NEGOTIABLE)

Every HTTP or MCP-mounted endpoint MUST have its authentication requirement
explicitly decided and implemented before merge. Comments of the form
"auth disabled for now, reinstate before production" MUST NOT be merged to
the default branch; if auth genuinely cannot be finished yet, the endpoint
MUST NOT be deployed or mounted at all. Secrets (tokens, client secrets,
keys) MUST NOT be committed to the repository, pasted into chat/agent
conversations, or left in ad hoc scratch files tracked by git. Any secret
that was exposed through an open endpoint, a committed file, or a chat
transcript MUST be treated as compromised and rotated, not merely re-hidden.
Rationale: this exact gap — `mcp_bearer_middleware` removed with a
"reinstate before production" comment that was never acted on — left the
production retrieval MCP server open to the internet. This principle exists
to make that class of failure structurally impossible, not just discouraged.

### IV. Structured, Loud Observability

Use `structlog` for all application logging (already the standard via
`log = structlog.get_logger(__name__)`). Risky or degraded configurations
(a fragile fallback mode, a missing recommended setting) MUST be surfaced
loudly at boot via a startup check — following the existing pattern in
`main.py:_log_citation_mode` — not discovered later via a silent failure in
production. Rationale: that boot check exists because a fragile fallback
already broke silently in production once; new fallback-prone code paths
should get the same treatment proactively, not reactively.

### V. Environment-Driven Configuration

All configuration (credentials, endpoints, feature flags) MUST flow through
the existing `pydantic-settings`-based settings classes (`ApiSettings`,
`get_runtime_settings`, etc.), sourced from environment variables / `.env`,
never hardcoded. Every new environment variable MUST be added to
`.env.example` with a comment explaining its purpose, in the same change
that introduces it. Rationale: `.env.example` is the only living
documentation of what configuration this system needs; letting it drift from
reality slows onboarding and incident response.

## Security & Secrets Requirements

Beyond Principle III: secrets storage is expected to move off plain `.env`
files toward a managed secret store (e.g. AWS Secrets Manager —
`secrets_client()` already exists, unused, in `pipeline/common/aws.py`) as
that migration lands. New secrets should be introduced in a way that's easy
to move to that store later, not one that deepens the `.env` dependency.
Network exposure (security groups, public URLs) MUST default to the
narrowest reasonable scope and be widened only deliberately, never left open
by default.

## Development Workflow

Non-trivial features (new endpoints, new auth flows, new external
integrations) MUST go through the spec-driven workflow
(`/speckit-specify` → `/speckit-plan` → `/speckit-tasks` →
`/speckit-implement`) rather than being designed ad hoc directly in code.
Small, isolated fixes (typos, one-line bug fixes) are exempt. Every feature's
plan MUST pass the Constitution Check gate before implementation begins.

## Governance

This constitution supersedes ad hoc convention when the two conflict.
Amendments are made via `/speckit-constitution` and MUST update the Sync
Impact Report and propagate to dependent templates (`plan-template.md`,
`spec-template.md`, `tasks-template.md`) in the same change. Versioning
follows semantic versioning: MAJOR for backward-incompatible principle
removal or redefinition, MINOR for new principles or materially expanded
guidance, PATCH for wording or clarification only. All plans produced by
`/speckit-plan` MUST include a Constitution Check section verifying
compliance with these principles, with any violation explicitly justified in
the plan's Complexity Tracking section.

**Version**: 1.0.0 | **Ratified**: 2026-07-08 | **Last Amended**: 2026-07-08
