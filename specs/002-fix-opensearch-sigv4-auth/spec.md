# Feature Specification: Fix OpenSearch IAM Auth Expiring Under Load

**Feature Branch**: `002-fix-opensearch-sigv4-auth`

**Created**: 2026-07-08

**Status**: Draft

**Input**: User description: "Fix OpenSearch IAM SigV4 authentication to use live/refreshable AWS credentials instead of a one-time frozen credential snapshot. Currently build_opensearch_client() in src/pipeline/common/opensearch.py calls boto3.Session().get_credentials().get_frozen_credentials() once at client construction time and bakes those static values into requests_aws4auth.AWS4Auth. Since IAM-role credentials (EC2 instance profile / ECS task role / EKS IRSA) are temporary STS-issued credentials that expire (typically within 1-6 hours), the frozen snapshot goes stale and every subsequent signed request fails with an auth error until the process is restarted (which re-fetches a fresh snapshot). This caused a production incident where OpenSearch auth appeared to fail 'out of the blue' and was only resolved by restarting the server. The fix: replace the frozen-credentials + requests_aws4auth.AWS4Auth approach with opensearchpy.AWSV4SignerAuth, passing it the live boto3 credentials object (not a frozen snapshot) so it re-signs each request from botocore's own auto-refreshing credential provider. This also removes the requests_aws4auth dependency in favor of opensearchpy's own signer (same library already used for the OpenSearch client), which delegates SigV4 signing to botocore.auth.SigV4Auth."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Long-running service keeps working past the IAM credential expiry window (Priority: P1)

Any long-running process that talks to OpenSearch using IAM-role credentials (the indexing pipeline, the retrieval/MCP service) must keep authenticating successfully indefinitely, without needing a manual or scheduled restart, as the underlying temporary AWS credentials rotate in the background.

**Why this priority**: This is the production incident itself. Without this fix, every long-running deployment silently breaks a few hours after startup and only recovers via manual intervention (a restart) — an operator has to notice the outage, diagnose it, and restart the process. This is the only story that must ship for the incident to be considered resolved.

**Independent Test**: Start the service, wait past the point where the ambient IAM credentials would have rotated (or force a credential rotation), and confirm OpenSearch requests continue to succeed with no restart and no manual intervention.

**Acceptance Scenarios**:

1. **Given** a service process connected to OpenSearch via an IAM role, **When** the role's temporary credentials expire and are rotated by AWS in the background, **Then** subsequent OpenSearch requests continue to authenticate successfully without any restart.
2. **Given** a service process that has been running for several hours under production load, **When** an operator checks OpenSearch request success rates, **Then** there is no correlation between elapsed process uptime and authentication failures.

---

### User Story 2 - Existing dev/local and other-environment auth paths keep working unchanged (Priority: P2)

Developers running against a local OpenSearch instance with username/password (basic auth), and any environment using explicit static AWS access keys instead of an IAM role, must see no change in behavior.

**Why this priority**: The fix is scoped to the IAM-role (SigV4) path; regressing the basic-auth dev path or static-credential path would block local development and other environments even though they were never affected by the incident.

**Independent Test**: Run the existing local/dev workflow against a localhost OpenSearch with basic auth and confirm it connects exactly as before.

**Acceptance Scenarios**:

1. **Given** `username`/`password` are both set, **When** the client is built, **Then** it connects using basic auth exactly as before, with no SigV4 signing involved.
2. **Given** an environment supplies static AWS access keys (not a role) via the standard AWS credential chain, **When** the client is built and used, **Then** requests continue to authenticate successfully.

---

### Edge Cases

- What happens when no AWS credentials are available at all (no IAM role, no static keys, no profile)? System must fail fast with a clear error at client-construction time, same as today.
- What happens if the IAM role's credentials are rotated *while a request is in flight*? The in-flight request must not be affected; only subsequent requests need to pick up the rotated credentials.
- What happens during the brief window when AWS is actively rotating the underlying credentials? Signing must transparently pick up valid credentials without surfacing a transient auth error to the caller.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST authenticate to OpenSearch using AWS IAM-role credentials in a way that remains valid indefinitely across credential rotations, without requiring a process restart.
- **FR-002**: System MUST NOT take a one-time, non-refreshing snapshot of temporary AWS credentials for the lifetime of a long-running process.
- **FR-003**: System MUST continue to support the existing basic-auth (username/password) connection mode for local/dev environments, unchanged.
- **FR-004**: System MUST fail fast with a clear, actionable error at client-construction time when no AWS credentials can be resolved at all (unchanged from current behavior).
- **FR-005**: System MUST NOT introduce any additional network calls or noticeable latency on the per-request path as a result of supporting credential refresh.
- **FR-006**: System SHOULD remove the now-unnecessary third-party SigV4-signing dependency once it is no longer used, to reduce dependency surface area.

### Key Entities

- **OpenSearch client factory**: The shared piece of code responsible for producing an authenticated OpenSearch client for both the indexing pipeline and the retrieval service, in either basic-auth or IAM-role mode.
- **AWS credential provider**: The live, auto-refreshing source of truth for the current AWS credentials (access key, secret key, session token) associated with the ambient IAM role, as distinct from a frozen point-in-time snapshot of those values.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A service instance connected to OpenSearch via an IAM role runs continuously for 24+ hours (spanning at least one full credential rotation) with zero authentication failures attributable to credential staleness.
- **SC-002**: Recovery from the class of incident experienced yesterday requires zero manual restarts going forward.
- **SC-003**: No measurable increase in per-request latency to OpenSearch as a result of this change.
- **SC-004**: Local/dev basic-auth workflows and any static-credential environments show no behavior change or new failures after the fix ships.

## Assumptions

- The production/EC2 (or ECS/EKS) environment continues to supply credentials via an ambient IAM role through the standard AWS credential provider chain — no change to how the role itself is attached or scoped.
- "Restart fixes it" was observed because a fresh process re-derives a new credential snapshot at startup; this confirms the credentials themselves (and the IAM role) were never the problem, only the failure to refresh them in-process.
- This fix is scoped to the shared OpenSearch client factory (`src/pipeline/common/opensearch.py`), which is the single implementation used by both the indexing pipeline and the retrieval service — no other AWS-facing code in the repository uses this frozen-credential pattern (confirmed by inspection).
- No other service in the codebase manually signs AWS requests outside of boto3's own service clients, so this fix does not need to touch S3, Secrets Manager, or other AWS integrations.
