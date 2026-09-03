# Implementation Plan: Fix OpenSearch IAM Auth Expiring Under Load

**Branch**: `002-fix-opensearch-sigv4-auth` | **Date**: 2026-07-08 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/002-fix-opensearch-sigv4-auth/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command. See `.specify/templates/plan-template.md` for the execution workflow.

## Summary

`build_opensearch_client()` (`src/pipeline/common/opensearch.py`) freezes IAM-role
credentials once via `get_frozen_credentials()` and bakes them into a static
`requests_aws4auth.AWS4Auth` signer, so signed requests fail once the underlying
STS-issued temporary credentials rotate — recoverable only by restarting the
process. Fix: replace `requests_aws4auth.AWS4Auth` with
`opensearchpy.AWSV4SignerAuth`, passing it the **live** boto3 credentials object
(`boto3.Session().get_credentials()`, not `.get_frozen_credentials()`), so botocore's
own `SigV4Auth` re-signs each request from a self-refreshing credential provider.
`requests-aws4auth` is then dropped as a dependency entirely.

## Technical Context

**Language/Version**: Python 3.11 (per `pyproject.toml` `requires-python`)

**Primary Dependencies**: `boto3>=1.34`, `opensearch-py>=2.6` (already provides
`opensearchpy.AWSV4SignerAuth`, added in 2.1.1 — no version bump needed);
`requests-aws4auth>=1.3` to be removed

**Storage**: N/A (this fix changes only how requests to OpenSearch are
authenticated, not data or indices)

**Testing**: `pytest` (existing `tests/unit/` suite); no existing unit tests
target `build_opensearch_client` directly — new tests added here

**Target Platform**: Linux server (EC2 / ECS / EKS running the indexing
pipeline and the retrieval/MCP service), both of which import
`build_opensearch_client` from the single shared module

**Project Type**: Single project (shared library module used by two
call sites: `pipeline/embedding_pipeline/indexer/opensearch.py` and
`retrieval/sources/opensearch.py`)

**Performance Goals**: No measurable per-request latency regression (SC-003);
credential refresh must stay a cheap in-memory expiry check, not a network
call on the hot path (matches boto3's own refresh design)

**Constraints**: Must not change behavior of the existing basic-auth
(dev/local) path; must fail fast with today's existing error message when no
AWS credentials resolve at all

**Scale/Scope**: Single-file fix (`src/pipeline/common/opensearch.py`) plus a
`pyproject.toml` dependency removal; no API, schema, or config changes

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Source-Agnostic Retrieval** — N/A. This fix does not add or change a
  logical retrieval source; OpenSearch remains registered exactly as today.
- **II. Reuse Before Building** — **Directly satisfied.** The fix itself *is*
  replacing a bolted-on third-party signer (`requests_aws4auth`) with the
  signer already bundled in `opensearch-py`, a dependency already in use for
  the client itself. No new abstraction is introduced.
- **III. No Endpoint Ships Without an Explicit Auth Decision** — N/A directly
  (this is outbound auth to OpenSearch, not an inbound HTTP/MCP endpoint), but
  in spirit this fix closes an auth gap the same way: production
  authentication silently degrading is exactly the class of failure this
  principle exists to prevent.
- **IV. Structured, Loud Observability** — Applies. Per the existing
  `_log_citation_mode`-style boot check pattern, client construction should
  log (via `structlog`) which auth mode was selected (basic vs. SigV4) at
  startup, so a misconfigured environment is visible immediately rather than
  discovered via a production auth failure.
- **V. Environment-Driven Configuration** — No new environment variables are
  introduced; `aws_region`, `username`, `password` continue to flow through
  existing settings. No `.env.example` changes needed.

No violations requiring justification — Complexity Tracking section is empty.

## Project Structure

### Documentation (this feature)

```text
specs/[###-feature]/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
src/
└── pipeline/
    └── common/
        └── opensearch.py          # build_opensearch_client() — the fix lands here, only

# Existing call sites, unchanged (just consumers of the fixed factory):
src/pipeline/embedding_pipeline/indexer/opensearch.py   # re-exports build_opensearch_client
src/retrieval/sources/opensearch.py                     # calls build_opensearch_client(...)

tests/
└── unit/
    └── pipeline/
        └── common/
            └── test_opensearch.py   # new — covers both auth modes + credential-refresh behavior
```

**Structure Decision**: Single project, single-file fix. `pipeline/common/opensearch.py`
is already the one shared factory both consumers import (confirmed by
inspection) — no restructuring needed, no new module or abstraction
introduced, per Constitution Principle II.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., 4th project] | [current need] | [why 3 projects insufficient] |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |
