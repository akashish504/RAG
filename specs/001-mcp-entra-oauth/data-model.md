# Phase 1 Data Model: MCP Server Sign-In via Organization Microsoft Account

Maps the spec's Key Entities (Organization Member, Access Session, Permitted
Group) to concrete record shapes. Storage per entity follows the decisions
in `research.md` §2–3: self-contained where possible, S3 where state must
be revocable/durable.

## AuthorizationCode

Represents a single, one-time-use exchange in progress between Microsoft's
redirect back to our server and the MCP client completing its own token
exchange. Corresponds to spec's "Access Session" while still being
established.

**Storage**: S3, `oauth/codes/{code}.json`. Deleted on exchange (single-use)
or once expired.

| Field | Type | Notes |
|---|---|---|
| `code` | string | Random, ≥128 bits entropy (S3 key) |
| `client_id` | string | The MCP client's registered client ID |
| `redirect_uri` | string | Where to send the client after completion |
| `code_challenge` | string | PKCE challenge from the original MCP client request |
| `scopes` | list[string] | Requested scopes |
| `user_oid` | string | Verified Entra object ID of the signed-in user |
| `user_email` | string | Verified email, for logging/display only — not an authorization key |
| `expires_at` | epoch seconds | Short TTL (minutes); enforced in code, not relied on for S3 lifecycle |

**Validation rules** (from FR-003, edge cases): a code MUST NOT be usable
twice (delete on read), MUST NOT be usable past `expires_at`, and exchange
MUST fail closed (no data returned) on any mismatch.

## AccessToken (self-contained, not stored)

Represents an active "Access Session" (spec). Not persisted — verified by
recomputing its signature.

| Field (in signed payload) | Type | Notes |
|---|---|---|
| `client_id` | string | Bound to the client that obtained it |
| `user_oid` | string | Verified Entra object ID |
| `scopes` | list[string] | Granted scopes |
| `exp` | epoch seconds | Short TTL (1 hour, per research.md §2) |
| signature | HMAC-SHA256 | Over the payload, keyed by `MCP_OAUTH_SIGNING_SECRET` |

**Validation rules** (FR-001, FR-003): any tool call MUST verify the
signature and `exp` before executing; on failure, reject without revealing
which check failed (avoid oracle behavior).

## RefreshToken

The revocable half of a session (spec Story 3 / SC-004) — deleting this
record is what makes revocation actually take effect once the current
access token expires.

**Storage**: S3, `oauth/refresh/{sha256(token)}.json` (hash as key so the
raw token value is never itself an S3 key/log-visible identifier).

| Field | Type | Notes |
|---|---|---|
| `token_hash` | string | SHA-256 of the raw refresh token (S3 key) |
| `client_id` | string | |
| `user_oid` | string | |
| `scopes` | list[string] | |
| `created_at` | epoch seconds | |
| `expires_at` | epoch seconds | Longer TTL than access tokens (e.g. days) |

**Validation rules** (FR-005, SC-004): deleting this record MUST cause the
next refresh attempt to fail; combined with the 1-hour access-token TTL,
this bounds total revocation delay to ≤ 1 hour as required.

**State transitions**: `active` → (used to mint a new access token; MAY
rotate, replacing the record) → `active` (rotated) | `revoked` (deleted,
terminal).

## ClientRegistration

Represents the MCP client (claude.ai's connector) as an OAuth client of our
authorization server. Registered once via the SDK's existing dynamic
client registration handler.

**Storage**: S3, `oauth/clients/{client_id}.json`.

| Field | Type | Notes |
|---|---|---|
| `client_id` | string | |
| `client_name` | string | As provided at registration |
| `redirect_uris` | list[string] | Allow-list checked on every `/authorize` call |
| `created_at` | epoch seconds | |

**Validation rules**: `/authorize` MUST reject any `redirect_uri` not in
this record's allow-list (standard OAuth open-redirect protection).

## Organization Member / Permitted Group (spec entities, no new storage)

Not modeled as application data at all — per research.md §5, membership is
enforced entirely by Entra ID's own "assignment required" + group
assignment on the Enterprise Application. Our system only ever sees users
Entra has already approved; there is no local user table.
