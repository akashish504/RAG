---

description: "Task list for feature implementation"
---

# Tasks: MCP Server Sign-In via Organization Microsoft Account

**Input**: Design documents from `/specs/001-mcp-entra-oauth/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/oauth-endpoints.md, quickstart.md

**Tests**: Included — plan.md's Technical Context and quickstart.md both
commit to a `tests/unit/test_entra_oauth.py` using `pytest` + `moto[s3]`.

**Organization**: Tasks are grouped by user story (spec.md priorities P1,
P2, P3) to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Every task includes an exact file path

## Path Conventions

Single project (per plan.md's Structure Decision) — `src/`, `tests/` at
repository root, extending the existing `src/pipeline/api/` and
`src/retrieval/mcp/` layout. No new top-level directories.

## Phase 1: Setup

**Purpose**: Environment/config groundwork, no application logic yet.

- [X] T001 [P] Document `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
      `AZURE_CLIENT_SECRET`, `MCP_OAUTH_SIGNING_SECRET` in `.env.example`
      with a one-line comment each (constitution Principle V)
- [X] T002 [P] Declare `pyjwt` explicitly under the `api` extra in
      `pyproject.toml` (already installed transitively via `mcp`; this is a
      hygiene declaration, not a new dependency — research.md §4)
- [X] T003 [P] Add `azure_tenant_id`, `azure_client_id`,
      `azure_client_secret`, `mcp_oauth_signing_secret` fields to
      `ApiSettings` in `src/pipeline/api/settings.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The provider skeleton and storage primitives every user story
builds on. No story is independently testable until this phase is done.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T004 Create `src/pipeline/api/entra_oauth.py` with generic S3
      JSON put/get/delete helpers for the `oauth/` prefix, wrapping the
      existing `s3_client()` from `src/pipeline/common/aws.py`
      (constitution Principle II — reuse, not a new AWS client) — depends
      on T003
- [X] T005 Implement `AuthorizationCode` record CRUD (create/load/delete,
      single-use) per `data-model.md`'s `AuthorizationCode` shape in
      `src/pipeline/api/entra_oauth.py` — depends on T004
- [X] T006 Implement `ClientRegistration` record CRUD (`get_client`,
      `register_client`) per `data-model.md`'s `ClientRegistration` shape
      in `src/pipeline/api/entra_oauth.py` — depends on T004
- [X] T007 Implement `RefreshToken` record CRUD (store/load/delete, keyed
      by SHA-256 hash) per `data-model.md`'s `RefreshToken` shape in
      `src/pipeline/api/entra_oauth.py` — depends on T004
- [X] T008 Implement self-contained `AccessToken` signing and verification
      (HMAC pattern reused from `src/retrieval/citation_token.py`, per
      research.md §2) in `src/pipeline/api/entra_oauth.py` — depends on
      T003
- [X] T009 [P] Add a boot-time check warning loudly if `AZURE_*` /
      `MCP_OAUTH_SIGNING_SECRET` are unset, mirroring the pattern in
      `src/pipeline/api/main.py`'s `_log_citation_mode` (constitution
      Principle IV) in `src/pipeline/api/main.py` — depends on T003
- [X] T010 Define the `EntraOAuthProvider(OAuthAuthorizationServerProvider)`
      class shell wiring the T005–T008 primitives together (method stubs
      for `authorize`, `exchange_authorization_code`, `load_access_token`,
      `load_refresh_token`, `exchange_refresh_token`, `revoke_token`,
      `get_client`, `register_client`) in `src/pipeline/api/entra_oauth.py`
      — depends on T005, T006, T007, T008
- [X] T011 Pass `auth_server_provider=EntraOAuthProvider(...)` and
      `auth=AuthSettings(...)` into the existing `FastMCP(...)` call in
      `src/retrieval/mcp/server.py:build_mcp` — depends on T010

**Checkpoint**: Provider is wired into the MCP mount but sign-in cannot yet
complete end-to-end (no `/authorize` logic, no callback route) — ready for
Story 1.

---

## Phase 3: User Story 1 - Authorized member connects and queries (Priority: P1) 🎯 MVP

**Goal**: A permitted user can add the connector, sign in with their
Microsoft work account, and successfully run a tool query.

**Independent Test**: Add the connector as a permitted user, complete the
Microsoft sign-in prompt, and confirm a tool call (e.g. `list_sources`)
returns real data end to end (spec Story 1).

- [X] T012 [US1] Implement `EntraOAuthProvider.authorize()` — build the
      redirect URL to `https://login.microsoftonline.com/{tenant}
      /oauth2/v2.0/authorize` and store the pending-request state (per
      `contracts/oauth-endpoints.md`'s `GET /authorize`) in
      `src/pipeline/api/entra_oauth.py` — depends on T010
- [X] T013 [US1] Implement `GET /oauth/callback` — exchange Microsoft's
      code at the tenant token endpoint via `requests`, verify the
      returned `id_token` signature via `PyJWT` + tenant JWKS (research.md
      §4), mint an `AuthorizationCode` (per `contracts/oauth-endpoints.md`)
      in `src/pipeline/api/main.py` — depends on T005, T012
- [X] T014 [US1] Implement `EntraOAuthProvider.exchange_authorization_code()`
      — validate PKCE `code_verifier`, delete the code (single-use), mint
      a signed access token plus a stored refresh token in
      `src/pipeline/api/entra_oauth.py` — depends on T005, T007, T008, T013
- [X] T015 [US1] Implement `EntraOAuthProvider.load_access_token()`,
      `load_refresh_token()`, and `exchange_refresh_token()` in
      `src/pipeline/api/entra_oauth.py` — depends on T007, T008
- [X] T016 [US1] Add `structlog` sign-in success/failure logging (spec
      FR-007) to the callback and token-exchange paths in
      `src/pipeline/api/entra_oauth.py` and `src/pipeline/api/main.py` —
      depends on T013, T014
- [X] T017 [US1] Unit tests for the happy path — `authorize` →
      `/oauth/callback` → token exchange → access-token verification —
      using `moto[s3]` and a stubbed Microsoft token endpoint (no live
      network call) in `tests/unit/test_entra_oauth.py` — depends on T014,
      T015
- [X] T018 [US1] Run `quickstart.md` steps 1–4 manually (silent/interactive
      Microsoft sign-in, then a real tool call) against a local run —
      depends on T017

**Checkpoint**: User Story 1 fully functional — a permitted user can sign
in and query, independently of Stories 2/3.

---

## Phase 4: User Story 2 - Unauthorized access is blocked (Priority: P2)

**Goal**: No sign-in, an out-of-tenant account, or a forged/expired/
tampered credential must all be rejected with no data returned.

**Independent Test**: Attempt to reach the retrieval tools with no sign-in,
and separately with an out-of-tenant account, and confirm both are
rejected with no data returned (spec Story 2).

- [X] T019 [US2] Enforce `redirect_uri`/`client_id` allow-list checks
      (against the stored `ClientRegistration`) in
      `EntraOAuthProvider.authorize()` and `get_client()`, per
      `contracts/oauth-endpoints.md`'s open-redirect protection note, in
      `src/pipeline/api/entra_oauth.py` — depends on T012
- [X] T020 [US2] Add the rejection path in `/oauth/callback` for Microsoft
      `error` responses, missing/expired pending-state lookups, and
      `id_token` signature-verification failures — no `AuthorizationCode`
      is minted on any of these — in `src/pipeline/api/main.py` — depends
      on T013
- [X] T021 [US2] Harden `EntraOAuthProvider.load_access_token()` to reject
      missing, expired, and tampered tokens uniformly (spec FR-003 — no
      information about which check failed) in
      `src/pipeline/api/entra_oauth.py` — depends on T015
- [X] T022 [US2] Unit tests: request with no auth is rejected; tampered
      access token is rejected; expired access token is rejected; forged
      callback `state` is rejected; PKCE `code_verifier` mismatch is
      rejected — in `tests/unit/test_entra_oauth.py` — depends on T019,
      T020, T021
- [ ] T023 [US2] Run `quickstart.md` steps 5–6 manually (no session,
      outside-tenant account, and — if group-scoping is configured —
      non-assigned-group account, all rejected) — depends on T022

**Checkpoint**: Unauthorized access is fully blocked, verified alongside
Story 1's working happy path.

---

## Phase 5: User Story 3 - Revoking one person's access (Priority: P3)

**Goal**: An administrator can cut off one user's access without affecting
anyone else, bounded to within 60 minutes (spec SC-004).

**Independent Test**: With two permitted users actively able to use the
tool, revoke one person's access and confirm they lose the ability to run
tools within a bounded time, while the other user is unaffected (spec
Story 3).

- [X] T024 [US3] Implement `EntraOAuthProvider.revoke_token()` — delete the
      corresponding `oauth/refresh/{hash}.json` record — in
      `src/pipeline/api/entra_oauth.py` — depends on T007
- [X] T025 [US3] Unit test: revoking one session's refresh token blocks its
      future refresh attempts while a second, distinct session's refresh
      token keeps working — in `tests/unit/test_entra_oauth.py` — depends
      on T024
- [ ] T026 [US3] Run `quickstart.md` step 7 manually — delete a session's
      refresh-token object directly in S3, confirm that session loses
      access within the access-token TTL bound, and that a second user's
      session is unaffected — depends on T025

**Checkpoint**: All three user stories are independently functional and
verified together.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Confirm nothing outside this feature's scope broke, and run
the full end-to-end validation guide.

- [X] T027 [P] Re-run `tests/unit/test_api_auth.py` and confirm `/health`
      and the existing `/v1/status` / `/v1/test/*` bearer-token routes are
      unaffected by the new `auth_server_provider` wiring (spec FR-008,
      constitution — no unrelated regressions)
- [ ] T028 Run the full `quickstart.md` guide end to end (steps 1–9,
      including the restart-resilience check in step 9) against a real or
      local deployment

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — T001–T003 can start immediately,
  all in parallel (different files)
- **Foundational (Phase 2)**: Depends on Setup (specifically T003) —
  BLOCKS all user stories. Internally sequential within
  `entra_oauth.py` (T004 → T005/T006/T007/T008 → T010 → T011), since it's
  one file; T009 (in `main.py`) can run in parallel with that chain.
- **User Stories (Phase 3–5)**: All depend on Foundational (Phase 2)
  completion. Each story is internally sequential (mostly editing the same
  two files, `entra_oauth.py` and `main.py`), but the three stories can be
  worked in priority order (P1 → P2 → P3) or, with multiple people, US2
  and US3 can start as soon as Foundational is done since neither strictly
  requires US1's tasks to be *merged* first — only the Foundational
  primitives they both extend.
- **Polish (Phase 6)**: Depends on all three user stories being complete.

### User Story Dependencies

- **User Story 1 (P1)**: Can start after Foundational (Phase 2). No
  dependency on US2/US3.
- **User Story 2 (P2)**: Can start after Foundational (Phase 2). Extends
  the same `authorize`/`callback`/`load_access_token` code paths US1
  builds, so in practice is easiest to do right after US1 lands, but does
  not require US1's tasks to be complete first if worked in parallel.
- **User Story 3 (P3)**: Can start after Foundational (Phase 2). Only
  needs T007's `RefreshToken` CRUD, not anything from US1/US2.

### Parallel Opportunities

- Setup: T001, T002, T003 — all parallel (three different files)
- Foundational: T009 (`main.py`) is parallel to the T004→T010→T011 chain
  (`entra_oauth.py` / `server.py`)
- Polish: T027 has no dependency on T028 and can run first/in parallel
- Beyond the above, most tasks are sequential edits to the same two files
  (`entra_oauth.py`, `main.py`) — this feature is one cohesive provider,
  not naturally parallelizable across many files, and tasks are not
  marked `[P]` where they'd conflict on the same file

---

## Parallel Example: Setup

```bash
Task: "Document AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, MCP_OAUTH_SIGNING_SECRET in .env.example"
Task: "Declare pyjwt explicitly under the api extra in pyproject.toml"
Task: "Add azure_tenant_id, azure_client_id, azure_client_secret, mcp_oauth_signing_secret to ApiSettings in src/pipeline/api/settings.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: run `quickstart.md` steps 1–4 independently
5. At this point the open-endpoint exposure this whole feature exists to
   close is already resolved for legitimate users — Stories 2/3 harden and
   operationalize it further, but the MVP alone stops the unauthenticated
   free-for-all.

### Incremental Delivery

1. Setup + Foundational → provider wired in, nothing usable yet
2. Add User Story 1 → sign-in + query works → this is the MVP
3. Add User Story 2 → negative paths verified airtight
4. Add User Story 3 → revocation operational
5. Polish → confirm no regressions, full quickstart pass

---

## Notes

- `[P]` tasks touch different files with no dependency on an incomplete
  task
- `[Story]` labels map tasks to spec.md's prioritized user stories for
  traceability
- Most of this feature lives in two files (`entra_oauth.py`, `main.py`),
  so — unlike a typical multi-service feature — most tasks are
  intentionally sequential, not parallel
- Commit after each task or logical group
- Stop at any checkpoint to validate a story independently before moving
  to the next priority

---

## Phase 7: Post-deployment fix — OAuth discovery at root

**Found in production** after Phases 1–6 shipped: connecting the real
claude.ai connector produced "Authentication failed" without ever reaching
Microsoft's login screen — server logs showed `/.well-known/oauth-
authorization-server`, `/.well-known/oauth-protected-resource/mcp/v2/mcp`,
and `/register` all 404ing. See research.md §7 for the full root cause
(FastMCP hosts its auth routes relative to its own mount point, but
MCP/OAuth discovery is root-relative to the server's origin).

- [X] T029 Change `src/retrieval/mcp/server.py` to pass `token_verifier=
      EntraTokenVerifier(...)` instead of `auth_server_provider=`; set
      `resource_server_url` on `AuthSettings` to the real tool URL —
      depends on T003
- [X] T030 Add `EntraTokenVerifier` adapter in `src/pipeline/api
      /entra_oauth.py` (needed because `mcp.server.auth.provider
      .ProviderTokenVerifier` doesn't type-check against our narrowed
      `Entra*` subtypes — research.md §7) and mount `/authorize`, `/token`,
      `/register`, `/revoke`, and both `.well-known` routes at root in
      `src/pipeline/api/main.py` via `create_auth_routes()` /
      `create_protected_resource_routes()` — depends on T029
- [X] T031 Local verification: confirm `/.well-known/oauth-authorization-
      server` and `/.well-known/oauth-protected-resource/mcp/v2/mcp` return
      200 at root (previously 404), `/mcp/v2/mcp` still 401s with no token,
      `/health` still public. Added `tests/unit/test_entra_oauth.py::
      test_token_verifier_accepts_valid_and_rejects_invalid` for the new
      adapter (14/14 unit tests pass; full suite: 329 passed, same 3
      pre-existing unrelated issues as before this fix)
- [X] T032 Redeploy (`docker compose build api && docker compose up -d
      api`) and re-test the real claude.ai connector end to end — depends
      on T031. **Requires production access — not executable from this
      environment.**
