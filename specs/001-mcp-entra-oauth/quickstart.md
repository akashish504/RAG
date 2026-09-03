# Quickstart: Validating MCP Server Sign-In via Organization Microsoft Account

Validation guide for this feature once implemented (see `tasks.md` for the
build steps, `contracts/oauth-endpoints.md` for endpoint shapes,
`data-model.md` for record shapes).

## Prerequisites

- Entra ID App Registration values in local `.env` (never in chat/commits —
  constitution Principle III): `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
  `AZURE_CLIENT_SECRET`.
- A generated `MCP_OAUTH_SIGNING_SECRET` (any high-entropy random string)
  in `.env`.
- `API_BEARER_TOKEN` still set (unrelated diagnostic routes keep using it).
- Local AWS credentials with access to `S3_BUCKET` (or run against
  `moto`-mocked S3 for the automated test path below).
- Entra ID Enterprise Application configured with "assignment required" +
  at least one test user assigned (research.md §5), if validating group
  scoping.

## Automated validation (unit tests)

```bash
pytest tests/unit/test_entra_oauth.py -v
pytest tests/unit/test_api_auth.py -v   # confirm existing bearer-auth routes unaffected
```

**Expected**: all pass. `test_entra_oauth.py` uses `moto[s3]` to mock the S3
calls and a stubbed Microsoft token endpoint (no live network call) to
cover: authorization code single-use, expired code rejection, access-token
signature/expiry verification, refresh-token revocation, and PKCE mismatch
rejection — one test per Functional Requirement in `spec.md`.

## Manual end-to-end validation

1. Run the API locally (`docker compose up api` or the existing local-dev
   entrypoint) with the prerequisites above set.
2. **Discovery check first** (research.md §7 — do this before adding the
   connector in claude.ai, since it catches the exact failure mode found
   in production): `curl https://<host>/.well-known/oauth-authorization-server`
   → expect `200` with `authorization_endpoint`/`token_endpoint`/etc. as
   **root-relative** URLs (e.g. `https://<host>/authorize`, not
   `https://<host>/mcp/v2/authorize`). If this 404s, the connector's OAuth
   flow will fail with "Authentication failed" before ever reaching
   Microsoft's login screen — check that `main.py:_mount_oauth_routes` is
   wired and that `retrieval/mcp/server.py:build_mcp` passes
   `token_verifier=`, not `auth_server_provider=`.
3. In claude.ai, add a Custom Connector pointing at the local/dev MCP URL.
   signed into a Microsoft session in the same browser (e.g. Teams/Outlook
   open), this should complete with at most one click (spec SC-003) — no
   password re-entry.
5. **Expect**: after sign-in, a tool call (e.g. "what sources are
   available?", which invokes `list_sources`) returns real data.
6. Sign out / use a browser profile with no Microsoft session, or use an
   account outside the tenant, and repeat step 3.
   **Expect**: sign-in fails or is rejected; no tool call succeeds (spec
   SC-001, Story 2).
7. If group-scoping is configured: attempt sign-in with a tenant account
   that is *not* in the assigned group.
   **Expect**: Entra itself blocks the sign-in before reaching this
   server's callback (research.md §5) — confirms zero-code enforcement.
8. **Revocation**: with an active session, delete that session's
   `oauth/refresh/{hash}.json` object directly in S3 (simulating an admin
   revoking access). Wait for the current access token to expire (up to 1
   hour) and confirm the client is forced to re-authenticate and cannot
   silently refresh (spec Story 3, SC-004).
9. **Health check unaffected**: `curl http://localhost:<port>/health` with
   no `Authorization` header at any point during the above.
   **Expect**: `200 {"status": "ok"}` throughout (spec FR-008).
10. **Restart resilience**: restart the API process mid-session.
    **Expect**: a still-valid (unexpired) access token keeps working with no
    re-verification cost (stateless), and refreshing still works (S3-backed
    refresh token survived the restart) — confirms the storage split in
    research.md §2–3 behaves as designed.
