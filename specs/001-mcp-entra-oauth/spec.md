# Feature Specification: MCP Server Sign-In via Organization Microsoft Account

**Feature Branch**: `001-mcp-entra-oauth`

**Created**: 2026-07-08

**Status**: Draft

**Input**: User description: "Secure the Dalberg MCP retrieval server (mounted at /mcp/v2/ in src/pipeline/api/main.py) with Microsoft Entra ID OAuth login, so that only authenticated members of the organization (the same Microsoft 365 / Entra ID tenant behind Teams and Outlook SSO) can access its tools (list_sources, get_schema, plan_retrieval, semantic_search, airtable_lookup) via the claude.ai web Custom Connector. Today the endpoint has no authentication at all and is reachable by anyone with the URL. Key context/constraints: the MCP client in use only supports OAuth-shaped auth (no static header/API key field), so login must be a real OAuth 2.1 flow; users should be able to sign in with their existing Microsoft/Entra ID credentials, ideally silently if already signed in elsewhere; access should optionally be restrictable to a specific Entra security group; unauthenticated or unauthorized requests must be rejected; a user whose access is revoked should lose access within a bounded, reasonably short time; the data platform being retired (Postgres/RDS) must not be used for any new persistent state; devops has already provisioned the Entra ID app registration needed; non-web MCP clients (Desktop, Code, curl) are out of scope."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Authorized team member connects and queries (Priority: P1)

A team member adds the retrieval tool as a connector in claude.ai. They sign
in using the same Microsoft work account they already use for Teams and
Outlook, and once signed in, can ask questions that use the retrieval tools
(e.g. list available sources, search for information) and get real results.

**Why this priority**: This is the core value of the feature — legitimate
users must still be able to do their job through the tool. Without this
working, the feature isn't shippable regardless of how well it blocks
unauthorized access.

**Independent Test**: Add the connector as a permitted user, complete the
Microsoft sign-in prompt, and confirm a tool call (e.g. listing available
sources) returns real data end to end.

**Acceptance Scenarios**:

1. **Given** a team member has never connected the tool before, **When**
   they add the connector and sign in with their Microsoft work account,
   **Then** they are able to successfully run a query and receive results.
2. **Given** a team member already has an active Microsoft sign-in session
   in their browser (e.g. from having Teams or Outlook open), **When** they
   add the connector, **Then** they are signed in without being asked to
   re-enter their password.
3. **Given** a team member's sign-in session has expired, **When** they
   attempt to use a tool, **Then** they are prompted to sign in again rather
   than silently failing or receiving stale/incorrect access.

---

### User Story 2 - Unauthorized access is blocked (Priority: P2)

Anyone who has not signed in, or who is not a member of the organization,
must not be able to retrieve any data through the tool, regardless of
whether they have the connector's URL.

**Why this priority**: This is the actual security requirement driving the
feature — the tool is currently open to anyone with the URL. Blocking
unauthorized access is what makes the feature worth building at all, ranked
second only because it depends on Story 1's sign-in flow existing first.

**Independent Test**: Attempt to reach the retrieval tools with no sign-in
at all, and separately with credentials for an account outside the
organization, and confirm both are rejected with no data returned.

**Acceptance Scenarios**:

1. **Given** no sign-in has occurred, **When** a request is made to any
   retrieval tool, **Then** the request is rejected and no data is returned.
2. **Given** someone attempts to sign in with a Microsoft account outside
   the organization's tenant, **When** they try to complete sign-in,
   **Then** they are not granted access to the tools.
3. **Given** a request is sent with a forged, expired, or tampered
   sign-in credential, **When** the server evaluates it, **Then** the
   request is rejected the same as if no credential were present.

---

### User Story 3 - Revoking one person's access (Priority: P3)

An administrator needs to stop a specific person from being able to use the
tool (e.g. they've left the team or the project), without disrupting access
for anyone else.

**Why this priority**: Important for ongoing operation and least-privilege
hygiene, but the feature still delivers its core value (Stories 1 and 2)
without it on day one; it governs what happens after initial rollout.

**Independent Test**: With two permitted users actively able to use the
tool, revoke one person's access and confirm they lose the ability to run
tools within a bounded time, while the other user is unaffected.

**Acceptance Scenarios**:

1. **Given** a user currently has access, **When** an administrator revokes
   that access, **Then** the user is unable to successfully run any tool
   within a bounded, short period of time.
2. **Given** one user's access has been revoked, **When** a different,
   still-permitted user makes a request, **Then** their access continues to
   work normally.

---

### Edge Cases

- What happens when a user's Microsoft sign-in session expires while a
  long-running task is in progress? They should be prompted to sign in
  again rather than silently losing access mid-task or continuing on stale
  credentials.
- What happens when Microsoft's identity service is temporarily unreachable
  during sign-in? The user should see a clear failure, not be granted
  access by default.
- What happens when someone tries to sign in with a personal (non-work)
  Microsoft account? They must not be granted access.
- What happens to a health-check/liveness request used by infrastructure
  monitoring? It must continue to succeed without requiring sign-in, so the
  service isn't reported as down due to an auth failure.
- What happens when a non-web MCP client (Desktop app, CLI, direct API
  call) attempts to connect? Out of scope for this feature — such clients
  are not required to be supported by this sign-in flow.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST require a successful sign-in before any retrieval
  tool executes; no tool may run on behalf of an unauthenticated request.
- **FR-002**: System MUST let users sign in with their existing
  organizational Microsoft account — the same one used for Teams and
  Outlook — without requiring a separate username/password created
  specifically for this tool.
- **FR-003**: System MUST reject any request lacking valid, unexpired
  sign-in proof, and MUST NOT reveal any information about available data
  sources or content in that rejection.
- **FR-004**: System MUST support restricting access to a defined subset of
  the organization (e.g. a specific team) rather than only being able to
  grant access to every member of the organization.
- **FR-005**: System MUST cause a user's access to stop working within a
  bounded, short period after their permission is revoked, without
  requiring the user to take any action themselves.
- **FR-006**: System MUST NOT introduce any new persistent state on the
  data platform currently being retired.
- **FR-007**: System MUST record security-relevant events (sign-in
  attempts, access denials) so they can be reviewed after the fact.
- **FR-008**: System MUST continue to allow infrastructure health checks to
  succeed without requiring sign-in.
- **FR-009**: Non-web MCP clients (desktop apps, command-line tools, direct
  API calls) are out of scope — this feature is not required to support a
  sign-in flow for them.

### Key Entities

- **Organization Member**: A person with a valid Microsoft work account in
  the organization's tenant; the pool of people who are potentially
  eligible to use the tool.
- **Access Session**: Time-bounded proof that a specific Organization
  Member is currently signed in and permitted to use the tools; expires and
  must be renewed periodically rather than lasting indefinitely.
- **Permitted Group**: An optional, explicitly defined subset of
  Organization Members who are allowed to use the tool, distinct from
  organization-wide membership.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of tool requests made without valid sign-in are rejected,
  with no data returned.
- **SC-002**: A permitted user can go from opening the connector to
  successfully completing their first query, including sign-in, in under 60
  seconds.
- **SC-003**: A user who is already signed into their Microsoft account
  elsewhere in the same browser completes sign-in to this tool with no more
  than one click and no password re-entry, at least 90% of the time.
- **SC-004**: When an administrator revokes a specific user's access, that
  user is unable to successfully run any tool within 60 minutes, and no
  other user's access is affected.
- **SC-005**: The infrastructure health-check endpoint remains reachable
  with 100% availability regardless of the sign-in system's status.

## Assumptions

- The bounded revocation time (Story 3, FR-005) is targeted at within 60
  minutes — a reasonable default balancing security against not requiring
  instant-revocation infrastructure. This can be tightened later if the
  business requires a stricter guarantee.
- Restricting access to a specific subset of the organization
  (FR-004/Permitted Group) is a should-have this feature must support, not
  necessarily enforced from day one — an initial rollout trusting the whole
  organization tenant is an acceptable starting configuration.
- The organization's Microsoft tenant is the sole source of truth for who
  counts as an authorized user; this feature does not introduce a separate
  user database.
- Non-web MCP clients (Desktop, Code, curl) are explicitly out of scope per
  the request; any separate access path for them is not addressed by this
  feature and would be a distinct future effort.
- "The data platform being retired" refers to the project's Postgres/RDS
  instance; this feature's persistent state needs live elsewhere.
