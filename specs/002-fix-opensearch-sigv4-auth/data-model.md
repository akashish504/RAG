# Phase 1 Data Model: Fix OpenSearch IAM Auth Expiring Under Load

This feature changes no persisted data, database schema, or index mapping.
The only "entities" are the in-memory objects involved in signing an
outbound OpenSearch request. Documented here for clarity since the spec's
Key Entities section names them.

## OpenSearch client factory

`build_opensearch_client(endpoint, *, username=None, password=None, aws_region="eu-west-1") -> OpenSearch`
(`src/pipeline/common/opensearch.py`)

- **Fields**: `endpoint` (host/URL), `username`/`password` (optional, mutually
  present/absent together), `aws_region`.
- **Behavior / state**: pure factory function — no persisted state of its
  own. Called once per process by each consumer (the indexer and the
  retrieval source), and the returned `OpenSearch` client is held for the
  lifetime of that consumer.
- **Change in this feature**: the *auth object* it builds and hands to the
  `OpenSearch` client changes from a frozen `AWS4Auth` snapshot to an
  `AWSV4SignerAuth` wrapping a live credentials reference. The function
  signature, return type, and both call sites are unchanged.

## AWS credential provider (live vs. frozen)

Two distinct values previously conflated by the bug — worth naming
explicitly since the whole fix hinges on the distinction:

- **Live credentials object** (`boto3.Session().get_credentials()`): a
  `RefreshableCredentials`-family object. Not a value — a handle that
  re-derives current access key/secret/session-token on each access,
  transparently refreshing from IMDS/STS shortly before expiry. **This is
  what must be held onto** for the life of the signer.
- **Frozen credentials snapshot** (`.get_frozen_credentials()` called on the
  above): a plain, immutable value object (access key, secret key, session
  token) valid only until the underlying temporary credentials it was read
  from expire. Correct for a single one-off signed operation; incorrect as
  the seed for a long-lived signer, which was the bug.

No state transitions apply beyond "credentials are currently valid" →
"credentials have expired and the live object has (or is about to have)
refreshed them" — a transition the live object already handles internally;
this feature's job is only to make sure the signer observes the live object
instead of a point-in-time copy of it.
