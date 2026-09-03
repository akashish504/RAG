# Phase 1 Contracts: OAuth & MCP Endpoints

Endpoints marked **(root, main.py)** are provided by `mcp.server.auth
.routes.create_auth_routes()` / `create_protected_resource_routes()`, but
mounted directly on the top-level FastAPI `app` at the **domain root** by
`main.py:_mount_oauth_routes` — not auto-mounted by `FastMCP` on its
`/mcp/v2/` sub-app. This correction (research.md §7) was made after a
production deployment showed 404s: MCP/OAuth discovery (RFC 8414 / RFC
9728) is root-relative to the resource server's origin, and `FastMCP`
would otherwise register these routes under `/mcp/v2/`, where no client
looks for them. They are not hand-written route bodies — the SDK's own
handler logic is reused, just mounted in the right place. The one route
this feature hand-writes from scratch is `/oauth/callback`.

## GET /.well-known/oauth-authorization-server **(root, main.py)**

Discovery metadata claude.ai's connector fetches automatically when adding
the connector. No auth required (metadata is public by definition).

**Response**: standard RFC 8414 fields — `issuer`, `authorization_endpoint`,
`token_endpoint`, `registration_endpoint`, `revocation_endpoint`,
`scopes_supported`, `code_challenge_methods_supported: ["S256"]` — all
root-relative (e.g. `authorization_endpoint: https://mcp.dev.dalberg.com
/authorize`, not `/mcp/v2/authorize`).

## GET /.well-known/oauth-protected-resource/mcp/v2/mcp **(root, main.py)**

RFC 9728 protected-resource metadata for the `/mcp/v2/mcp` tool endpoint —
tells a client which authorization server protects this resource. Added
alongside the fix above (was previously not mounted at all, since
`resource_server_url` was originally left unset to sidestep this exact
mounting question — the underlying mismatch needed fixing regardless).

**Response**: `{resource: ".../mcp/v2/mcp", authorization_servers: [
"https://mcp.dev.dalberg.com/"], scopes_supported, bearer_methods_supported:
["header"]}`.

## POST /register **(root, main.py)**

Dynamic client registration — claude.ai registers itself as an OAuth client
the first time the connector is added.

**Request**: `{client_name, redirect_uris, ...}` (RFC 7591 shape).
**Response**: `{client_id, redirect_uris, ...}` — persisted via
`EntraOAuthProvider.register_client` → `oauth/clients/{client_id}.json`
(data-model.md `ClientRegistration`).

## GET /authorize **(root, main.py, calls into our provider)**

Entry point for the MCP client's OAuth flow. Our
`EntraOAuthProvider.authorize()` builds and returns the redirect target.

**Request query params**: `client_id`, `redirect_uri`, `code_challenge`,
`code_challenge_method=S256`, `state`, `scope`.

**Behavior**: validates `client_id`/`redirect_uri` against the stored
`ClientRegistration`; if valid, responds with a `302` to
`https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize`, with our
own `state` value encoding enough to reconstruct the original request in
the callback (e.g. an S3-stored pending-request record keyed by that
state, mirroring `AuthorizationCode`'s shape but pre-Microsoft-login).

## GET /oauth/callback **(NEW — hand-written, this feature)**

Microsoft's redirect target after the user completes sign-in. Registered
as the app's redirect URI in the Entra App Registration.

**Request query params** (from Microsoft): `code`, `state`, or `error` /
`error_description` on failure.

**Behavior**:
1. Look up the pending-request record by `state`; `400` if missing/expired
   (protects against forged/replayed callbacks — edge case in spec).
2. Exchange `code` at Microsoft's token endpoint (`requests`, server-side,
   using `AZURE_CLIENT_SECRET`) for Microsoft's `id_token`.
3. Verify `id_token` signature via `PyJWT` + tenant JWKS (research.md §4).
4. Mint our own `AuthorizationCode` (data-model.md), store it in
   `oauth/codes/`.
5. `302` redirect to the original MCP client's `redirect_uri` with our
   `code` and the original `state`.

**Failure behavior**: on any verification failure, redirect to the client
with an OAuth `error` param (per spec, no data exposed) — never grant a
code on a Microsoft error or signature-verification failure.

## POST /token **(root, main.py, calls into our provider)**

The MCP client exchanges our authorization code (or a refresh token) for
an access token.

**Request** (`grant_type=authorization_code`): `code`, `client_id`,
`code_verifier` (PKCE), `redirect_uri`.
→ `EntraOAuthProvider.exchange_authorization_code`: validates PKCE, deletes
the `AuthorizationCode` record (single-use), mints and returns a signed
`AccessToken` (data-model.md — not stored) + a new `RefreshToken` (stored).

**Request** (`grant_type=refresh_token`): `refresh_token`, `client_id`.
→ `EntraOAuthProvider.exchange_refresh_token`: looks up
`oauth/refresh/{hash}.json`; `invalid_grant` if missing/expired/revoked;
otherwise mints a new `AccessToken` (and, per rotation policy, a new
`RefreshToken`).

**Response**: standard RFC 6749 token response —
`{access_token, token_type: "Bearer", expires_in, refresh_token, scope}`.

## POST /revoke **(root, main.py, calls into our provider)**

`EntraOAuthProvider.revoke_token` deletes the corresponding
`oauth/refresh/{hash}.json` record. (Access tokens can't be individually
revoked — see research.md §2 trade-off — so revocation here targets the
refresh token, bounding total effective revocation delay to the access
token's TTL.)

## POST /mcp/v2/mcp (existing MCP tool-call endpoint — auth now enforced)

**Request header**: `Authorization: Bearer <access_token>`, required on
every call (spec FR-001).

**Behavior change from today**: FastMCP's bearer-auth middleware, wired via
`token_verifier=EntraTokenVerifier(...)` (research.md §7 — deliberately not
`auth_server_provider=`, which would also mis-mount the routes above),
calls `EntraOAuthProvider.load_access_token()` (signature + `exp` check, no
I/O) before any tool (`list_sources`, `get_schema`, `plan_retrieval`,
`semantic_search`, `airtable_lookup`) executes. Missing/invalid/expired
token → `401`, no tool invoked, no data returned (spec SC-001). This
enforcement is independent of where `/authorize`/`/token`/etc. are mounted
— it only depends on `token_verifier` being wired into `FastMCP(...)`.

## GET /health (unchanged)

Remains public, no auth — required for the load balancer (spec FR-008).
Not part of the `/mcp` or `/oauth` path prefixes, so unaffected by this
feature.
