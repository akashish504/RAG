# Phase 0 Research: MCP Server Sign-In via Organization Microsoft Account

All items below were resolved during planning (no unresolved
`NEEDS CLARIFICATION` markers remain in Technical Context).

## 1. Overall OAuth architecture

**Decision**: The MCP server acts as a thin OAuth 2.1 authorization server
(implementing `mcp.server.auth.provider.OAuthAuthorizationServerProvider`,
passed directly into the existing `FastMCP(...)` constructor via
`auth_server_provider=`) that delegates actual identity verification to
Microsoft Entra ID as an upstream OIDC provider.

**Rationale**: The claude.ai web Custom Connector only knows how to be an
OAuth *client* against whatever server it's pointed at — it has no concept
of Microsoft Entra ID specifically, and no field for a static credential.
The `mcp` SDK already installed (`mcp>=1.6`, resolved to 1.27.1) ships this
exact two-hop pattern out of the box (`OAuthAuthorizationServerProvider`'s
docstring literally diagrams Client ↔ MCP Server ↔ 3rd-party OAuth Server),
so this is "implement one provider class," not "build an OAuth server from
scratch."

**Alternatives considered**: A reverse-proxy session-cookie gate (e.g.
`oauth2-proxy`) — rejected because claude.ai's connector does a token-based
OAuth handshake directly with the MCP server, not a browser-cookie session;
a cookie-based proxy doesn't intercept that handshake correctly.

## 2. Access token format

**Decision**: Self-contained, HMAC-signed access tokens — same pattern
already implemented in `retrieval/citation_token.py` (base64url payload +
HMAC-SHA256 signature, embedded expiry), extended to carry `client_id`, the
verified user's `oid`/`email`, granted scopes, and `exp`. Verified by
recomputing the signature — no storage lookup.

**Rationale**: Access tokens are checked on *every* MCP tool call. A
storage-backed opaque token (S3 lookup per call) would add a network
round-trip to every single tool invocation; a self-contained signed token
verifies in-process. The exact signing/verification shape already exists
and is tested in this codebase, satisfying "Reuse Before Building."

**Alternatives considered**: Standard JWT via PyJWT (already available) —
functionally equivalent, but the existing `citation_token.py` format is
simpler (no JOSE header/alg-confusion surface) and already proven in this
codebase; no reason to introduce a second token format for the same job.
Opaque token + S3 lookup per call — rejected for the latency reason above.

**Trade-off accepted**: self-contained tokens can't be revoked individually
before they expire. Mitigated by a short TTL (1 hour) plus revocable,
S3-backed refresh tokens — see spec SC-004 (60-minute revocation bound).

## 3. State storage (codes, refresh tokens, client registrations)

**Decision**: S3, using the existing `S3_BUCKET` and the existing
`s3_client()` helper in `pipeline/common/aws.py`, under a new `oauth/`
prefix (`oauth/codes/`, `oauth/refresh/`, `oauth/clients/`).

**Rationale**: Postgres/RDS is being retired and is explicitly excluded
(spec FR-006). These three record types are all low-frequency (once per
login, roughly hourly per session, once per client registration
respectively), so S3's per-request latency doesn't matter the way it would
for the access-token hot path. Reuses existing AWS access patterns instead
of introducing a new storage dependency.

**Alternatives considered**: In-memory dict — rejected, loses all sessions
on every deploy/restart and breaks outright if the service ever runs on
more than one instance. A new lightweight DB (SQLite/DynamoDB) — rejected
as unnecessary; S3 already covers the requirement at the required
frequency, and adding a new storage *technology* (vs. reusing S3, already
provisioned and already wrapped) would cut against Reuse Before Building
for no real benefit at this scale.

## 4. Verifying Microsoft's identity assertion

**Decision**: After exchanging Microsoft's authorization code at the tenant
token endpoint (server-to-server, via `requests` — already a hard
dependency through the `api` extra), verify the returned `id_token`'s
signature using `PyJWT` against the tenant's published JWKS (discovered via
the standard `/.well-known/openid-configuration` document).

**Rationale**: `PyJWT` is already installed as a transitive dependency of
`mcp` itself (confirmed via `pip show mcp` → `Requires: ... pyjwt ...`), so
this adds no new dependency — just an explicit declaration in `pyproject
.toml` for hygiene (Reuse Before Building). Verifying the signature (rather
than trusting the payload unchecked) is the correct default even though the
exchange happens over a server-to-server TLS channel, since it's a small,
one-time cost per login and removes any reliance on transport trust alone.

**Alternatives considered**: Decode without verifying signature (trust the
direct TLS channel to Microsoft) — rejected; verification is cheap and
already-available, so there's no real cost to doing it properly.

## 5. Restricting access to a subset of the organization (Permitted Group)

**Decision**: Enforce group-scoping using Entra ID's built-in "assignment
required" setting on the Enterprise Application, with the desired security
group assigned to it (Azure Portal configuration, no application code).
Entra rejects unassigned users during its own sign-in step, before our
callback ever executes.

**Rationale**: This satisfies spec FR-004 with zero code. Parsing a
`groups` claim from the token ourselves has a well-known pitfall
("claims overage" — Microsoft omits the claim and requires a separate
Microsoft Graph API call once a user belongs to more than ~200 groups),
which is unnecessary complexity for a requirement Entra already handles
natively.

**Alternatives considered**: Reading the `groups` claim in the id_token and
checking membership in our provider — rejected due to the overage edge
case and the extra Graph API dependency it would sometimes require for no
added benefit over the built-in enforcement.

## 6. HTTP client for the Microsoft token exchange

**Decision**: `requests`, already a hard dependency via the `api` extra.

**Rationale**: `httpx` is only present transitively (pulled in by `mcp`
itself); depending on it explicitly without declaring it would be relying
on another package's internal dependency, which is fragile if `mcp` ever
drops it. `requests` is already declared and used elsewhere in this
codebase for exactly this kind of server-to-server call (see the Airtable
and Anthropic integrations).

## 7. OAuth route mounting: root, not under /mcp/v2/

**Decision**: Host `/authorize`, `/token`, `/register`, `/revoke`, and both
`.well-known` discovery endpoints directly on the top-level FastAPI `app`,
at the domain root — not on the FastMCP sub-app mounted at `/mcp/v2/`.
`FastMCP` is wired with `token_verifier=` only (bearer-token enforcement on
the tool endpoint, via the `EntraTokenVerifier` adapter in
`entra_oauth.py`), not `auth_server_provider=` (which would make it also
host — and mis-locate — the OAuth routes).

**Rationale**: discovered in production, post-deployment — connecting the
claude.ai connector produced "Authentication failed" without ever reaching
Microsoft's login screen. Server logs showed `GET
/.well-known/oauth-authorization-server`, `GET
/.well-known/oauth-protected-resource/mcp/v2/mcp`, and `POST /register` all
returning 404. Root cause: `mcp.server.fastmcp.FastMCP` registers its auth
routes relative to wherever it's mounted, but MCP/OAuth discovery (RFC 8414
§3, RFC 9728 §3.1) is specified as root-relative to the resource server's
origin, regardless of the protected resource's own path. Real clients
request those paths at the bare domain and get nothing if the routes only
exist under `/mcp/v2/`. Missed during initial implementation and local
verification because that verification used a hand-crafted bearer token
directly against `/mcp/v2/mcp`, never exercising the discovery-driven flow
a real OAuth client performs end to end.

**Fix verified locally** (see quickstart.md): with the fix,
`/.well-known/oauth-authorization-server` and
`/.well-known/oauth-protected-resource/mcp/v2/mcp` both return 200 with
root-relative endpoint URLs; `/mcp/v2/mcp` still correctly 401s with no
token; `/health` remains public.

**Alternatives considered**: mounting the whole FastMCP app at the domain
root instead of `/mcp/v2/` — rejected: would change the tool endpoint's
public URL from `/mcp/v2/mcp` to `/mcp`, a breaking change for the
connector config already given to the team, and would abandon the
`/mcp/v2/` versioning prefix the codebase deliberately adopted when the old
`/mcp/` (v1) mount was removed. Using `mcp.server.auth.provider
.ProviderTokenVerifier` for the `token_verifier=` wiring — rejected: it's
typed against the generic base `OAuthAuthorizationServerProvider
[AuthorizationCode, RefreshToken, AccessToken]`, which `EntraOAuthProvider`
doesn't satisfy under strict parameter variance (its `Entra*` subtypes
narrow the parameter types on `exchange_authorization_code` /
`exchange_refresh_token`); a small dedicated `EntraTokenVerifier` adapter
avoids the type mismatch since it only needs `load_access_token`.
