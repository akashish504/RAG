# Implementation Plan: MCP Server Sign-In via Organization Microsoft Account

**Branch**: `001-mcp-entra-oauth` | **Date**: 2026-07-08 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-mcp-entra-oauth/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command. See `.specify/templates/plan-template.md` for the execution workflow.

## Summary

The retrieval MCP server at `/mcp/v2/` (`src/pipeline/api/main.py`) currently
has no authentication. The claude.ai web Custom Connector — the only client
in scope — drives an OAuth-shaped login, so the fix is to make the MCP
server an OAuth 2.1 authorization server that delegates identity to the
org's Microsoft Entra ID tenant (the same one behind Teams/Outlook SSO).
Technical approach: implement `mcp.server.auth.provider
.OAuthAuthorizationServerProvider` (already supported directly by the
`FastMCP` constructor already in use) with Microsoft as the upstream IdP;
issue short-lived, self-contained signed access tokens (no per-request
storage lookup, reusing the exact HMAC pattern already in
`retrieval/citation_token.py`); persist only the low-frequency OAuth state
(authorization codes, refresh tokens, client registration) in S3, since
Postgres/RDS is being retired and must not gain new usage.

## Technical Context

**Language/Version**: Python 3.13 (matches the existing `.venv`; repo's
`ruff` target is py311-compatible syntax)

**Primary Dependencies**: FastAPI + the `mcp` SDK (`mcp>=1.6`, installed
1.27.1) already used by `retrieval/mcp/server.py`; `mcp.server.auth
.provider.OAuthAuthorizationServerProvider` for the provider protocol.
`requests` (already a hard dependency via the `api` extra) for the
server-to-server call to Microsoft's token endpoint. `PyJWT` for verifying
Microsoft's signed `id_token` — already installed as a transitive
dependency of `mcp` itself (`pip show mcp` → `Requires: ... pyjwt ...`), so
this adds no new dependency, just an explicit declaration for hygiene.
`boto3` (already a hard dependency) via the existing `s3_client()` /
`secrets_client()` helpers in `pipeline/common/aws.py`.

**Storage**: S3 only (existing bucket `S3_BUCKET=claude-mcp-object-store`,
new `oauth/` prefix) for authorization codes, refresh tokens, and dynamic
client registration records. No database — Postgres/RDS is being retired
and explicitly excluded (spec FR-006). Access tokens are self-contained
(HMAC-signed, no storage).

**Testing**: `pytest` (existing `tests/unit/`), `fastapi.testclient
.TestClient` (see `tests/unit/test_api_auth.py` for the existing pattern
this extends), `moto[s3]` (already a `dev` dependency) to mock S3 without
hitting real AWS, and a mocked/stubbed Microsoft token endpoint for the
Entra exchange (no live Microsoft call in unit tests).

**Target Platform**: Linux server — single EC2 instance behind nginx,
deployed via the existing `docker-compose.yml` / `Dockerfile.api`.

**Project Type**: Single project — web service (FastAPI backend), no
frontend/mobile component.

**Performance Goals**: Access-token validation (on every MCP tool call)
must add zero network/storage I/O — signature verification only. Sign-in
(one-time per session, includes a browser redirect to Microsoft) has no
hard latency target beyond spec SC-002 (< 60s including sign-in).

**Constraints**: Must work within the claude.ai web Custom Connector's
OAuth-only flow (no static-header option); MUST NOT add Postgres/RDS usage;
access revocation must take effect within 60 minutes (spec SC-004) without
requiring the user to act; `/health` must stay reachable with no auth for
the load balancer (spec FR-008).

**Scale/Scope**: Single organization's Entra ID tenant; expected dozens,
not thousands, of concurrent user sessions; single EC2 instance (no
multi-instance/HA requirement today, but S3-backed state means it would
survive a move to multiple instances without redesign).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How this plan satisfies it |
|---|---|---|
| I. Source-Agnostic Retrieval | PASS | This feature wraps the whole `/mcp/v2/` mount at the transport/auth layer; it does not touch `retrieval/config`, the source registry, or any per-source logic. No source-specific auth branching. |
| II. Reuse Before Building | PASS | Reuses `s3_client()`/`secrets_client()` (`pipeline/common/aws.py`), the signed-token pattern from `retrieval/citation_token.py` for access tokens, `requests` (already a dependency) for the Microsoft token call, and `PyJWT` (already installed transitively via `mcp`, just declared explicitly). The only new file is `entra_oauth.py`, justified because no existing code implements an `OAuthAuthorizationServerProvider`. |
| III. No Endpoint Ships Without an Explicit Auth Decision | PASS | This is the feature closing that exact gap. New routes (`/oauth/callback`, `/oauth/token`, etc., provided by the `mcp` SDK's auth routes) are auth-flow endpoints by design — publicly reachable is correct for them, same as any OAuth authorization server, not an oversight. |
| IV. Structured, Loud Observability | PASS (new work required) | Sign-in success/failure/revocation events logged via `structlog` (spec FR-007). A boot-time check (mirroring `main.py:_log_citation_mode`) MUST warn loudly if `AZURE_*` env vars are missing, rather than failing silently on first login attempt. |
| V. Environment-Driven Configuration | PASS (new work required) | New settings (`azure_tenant_id`, `azure_client_id`, `azure_client_secret`, `mcp_oauth_signing_secret`) added to `ApiSettings`, sourced from env, documented in `.env.example` in the same change. |
| Security & Secrets Requirements | PASS | New secrets go through the settings layer (movable to Secrets Manager later, per the constitution's stated direction), not scattered `os.environ` calls. Does not change network-exposure scope (out of scope for this feature, per spec). |

No violations requiring justification — Complexity Tracking is empty.

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
├── pipeline/
│   └── api/
│       ├── main.py            # EDIT: mount /oauth/callback + root OAuth
│       │                      #   routes (authorize/token/register/revoke/
│       │                      #   .well-known — research.md §7), boot check
│       ├── settings.py        # EDIT: add azure_*, mcp_oauth_signing_secret,
│       │                      #   mcp_public_base_url; oauth_issuer_url is
│       │                      #   the domain root (§7), not /mcp/v2
│       ├── auth.py            # unchanged (existing bearer-token dep, kept
│       │                      #   for the non-MCP /v1/test/* diagnostic routes)
│       └── entra_oauth.py     # NEW: EntraOAuthProvider + EntraTokenVerifier
│                               #   + S3-backed CRUD for codes/refresh-tokens/
│                               #   client records
└── retrieval/
    ├── citation_token.py      # unchanged; access-token signing reuses
    │                          #   this exact HMAC pattern
    └── mcp/
        └── server.py          # EDIT: pass token_verifier= / auth= (NOT
                                #   auth_server_provider= — see research.md
                                #   §7) into the existing FastMCP(...) call

tests/
└── unit/
    └── test_entra_oauth.py    # NEW: provider unit tests (moto-mocked S3,
                                #   stubbed Microsoft token endpoint)

.env.example                   # EDIT: document new AZURE_*/MCP_OAUTH_* vars
```

**Structure Decision**: Single project (this is the existing FastAPI
backend, no separate frontend/mobile component). The feature adds one new
module (`entra_oauth.py`) rather than a new top-level package, since it's a
single cohesive concern (one provider class + its S3-backed storage) that
plugs into the existing `pipeline/api` and `retrieval/mcp` structure without
needing its own directory.

## Complexity Tracking

*No entries — Constitution Check above has no unjustified violations.*
