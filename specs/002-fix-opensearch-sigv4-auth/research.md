# Phase 0 Research: Fix OpenSearch IAM Auth Expiring Under Load

No `[NEEDS CLARIFICATION]` markers remain in the Technical Context — this
feature is a scoped bug fix with a well-understood root cause. This document
records the technology decision and the alternatives considered.

## Decision: Root cause

**Finding**: `build_opensearch_client()` (`src/pipeline/common/opensearch.py:63-76`)
calls `boto3.Session().get_credentials().get_frozen_credentials()` once, at
client-construction time, and passes the resulting static access
key/secret/session-token strings into `requests_aws4auth.AWS4Auth`. IAM-role
credentials (EC2 instance profile / ECS task role / EKS IRSA) are temporary,
STS-issued credentials with a real expiry (commonly ~1–6 hours depending on
role/session config). Because the snapshot is frozen once and the
`AWS4Auth` instance is reused for the OpenSearch client's entire process
lifetime, every request signed after the snapshot's expiry fails
authentication — indistinguishable from a rotated/invalid key from the
caller's point of view. A process restart re-runs `build_opensearch_client()`,
grabbing a fresh (temporarily valid) snapshot, which is why a restart
"fixed" the incident without anything about the IAM role actually changing.

**Rationale**: Confirmed by reading `botocore`'s credential-refresh design —
`boto3.Session().get_credentials()` returns a `RefreshableCredentials` /
`DeferredRefreshableCredentials` object that re-checks its own expiry on
every access and transparently fetches new credentials from
IMDS/STS shortly before expiry, but only if callers keep calling into
*that live object*. `get_frozen_credentials()` is explicitly a one-time,
non-refreshing snapshot — appropriate for a single signed operation, not for
seeding a long-lived signer.

## Decision: Replacement signer

**Chosen**: `opensearchpy.AWSV4SignerAuth`, constructed with the **live**
`boto3.Session().get_credentials()` object (not frozen), passed as
`http_auth` to the existing `OpenSearch(...)` client construction.

**Rationale**:
- Delegates actual SigV4 signing to `botocore.auth.SigV4Auth` — the same
  signer boto3/botocore uses internally for every AWS API call, rather than
  a third-party reimplementation of the SigV4 spec.
- Ships inside `opensearch-py` (`>=2.1.1`; repo already pins `>=2.6`), which
  is already a direct dependency used for the `OpenSearch` client itself —
  no version bump required, and it removes the separate `requests-aws4auth`
  dependency entirely, satisfying Constitution Principle II (Reuse Before
  Building) and reducing dependency surface area.
- Because it holds a reference to the live credentials object rather than a
  frozen snapshot, each request re-derives current credentials from
  `RefreshableCredentials`, which is a cheap local expiry check on the
  overwhelmingly common case (an in-memory timestamp comparison under a
  lock) and only performs a real network fetch during the ~15-minute
  advisory-refresh window before expiry. This satisfies the "no per-request
  latency regression" constraint (SC-003) — see Alternatives Considered
  below for the comparison that ruled out a naive rebuild-per-request
  approach.
- Forward-compatible with OpenSearch Serverless (`service="aoss"`) should
  this codebase ever point at a serverless collection, which
  `requests_aws4auth` has no concept of. Not a current requirement, but a
  zero-cost side benefit of the switch.

## Alternatives considered

1. **`requests_aws4auth.AWS4Auth.from_credentials(live_credentials, ...)`** —
   also fixes the bug (it re-derives frozen credentials from the live object
   per request rather than freezing once at construction). Rejected in favor
   of `AWSV4SignerAuth` because it's a third-party, generic-AWS reimplementation
   of SigV4 outside the OpenSearch ecosystem, and keeping it would mean
   carrying a dependency (`requests-aws4auth`) that provides no capability
   `opensearch-py`'s own signer doesn't already cover.
2. **Rebuild the `OpenSearch` client (and re-freeze credentials) on a timer
   or on every request** — rejected: reinvents, less efficiently and with
   more moving parts, exactly what `RefreshableCredentials` already does
   internally; would add real per-request or per-interval overhead instead
   of the near-zero-cost expiry check the live-object approach gives for
   free.
3. **Catch auth errors and reconnect/retry** — rejected as a workaround, not
   a fix: it would mask the same root cause (stale frozen credentials) with
   retry logic instead of removing the staleness, and would still cause a
   window of failed requests (and added latency/complexity) on every
   rotation instead of zero failures.

## Output

All unknowns resolved; no `[NEEDS CLARIFICATION]` markers remain. Proceeding
to Phase 1 (data-model.md, contracts/, quickstart.md).
