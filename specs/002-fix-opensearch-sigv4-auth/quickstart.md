# Quickstart: Validate the OpenSearch IAM Auth Fix

## Prerequisites

- Repo checked out on branch `002-fix-opensearch-sigv4-auth`, dependencies
  installed (`opensearch-py>=2.6` already present; `requests-aws4auth`
  removed as part of this fix).
- AWS credentials available via an assumable role or `~/.aws/credentials`
  profile for manual/integration checks (unit tests below do not need real
  AWS access — they mock `boto3.Session`).

## 1. Unit tests (fast, no AWS access required)

Run the new/updated tests for `build_opensearch_client`:

```bash
pytest tests/unit/pipeline/common/test_opensearch.py -v
```

**Expected outcomes**, per `contracts/build-opensearch-client.md`:

- Basic-auth path (`username`/`password` set) still builds a client with
  `http_auth=(username, password)` — unchanged from before the fix.
- SigV4 path (no username/password) builds a client whose `http_auth` is an
  `opensearchpy.AWSV4SignerAuth` instance holding the **live** credentials
  object returned by `boto3.Session().get_credentials()` — assert this is
  *not* a frozen/static credentials snapshot (e.g. by mocking
  `boto3.Session().get_credentials()` to return a fake refreshable object
  and asserting the factory never calls `.get_frozen_credentials()` on it
  itself).
- No-credentials path still raises `ValueError` with the existing message.

## 2. Regression check — credential rotation across process lifetime

This is the scenario that caused the original incident; simulate it without
waiting hours for a real IAM rotation:

1. Mock (or use a fake) boto3 credentials object whose `access_key` /
   `secret_key` / `token` change value between two calls to
   `.get_frozen_credentials()` — i.e., simulate what botocore does
   internally when it refreshes.
2. Build the client once via `build_opensearch_client(...)`.
3. Simulate two signed requests using the returned `http_auth`, with the
   fake credentials object reporting different values (in place of an
   expiring value) between the two.
4. Assert both requests are signed using whatever the credentials object
   currently reports at sign-time — proving the signer reads live, not
   frozen, credentials. (Prior to the fix, this would fail: the second
   signature would still reflect the first snapshot.)

## 3. Manual / staging verification (optional, requires real AWS + OpenSearch)

1. Deploy the fixed build to a long-running environment (staging EC2/ECS
   task) with the ambient IAM role attached.
2. Confirm normal OpenSearch queries succeed immediately after startup.
3. Either wait past one full credential rotation window for that role
   (commonly ~1 hour, check the specific role's session duration), or force
   a rotation if your environment supports it, then re-issue a query.
4. **Expected**: query still succeeds, no restart performed. Compare against
   pre-fix behavior, where this same wait would have produced an auth
   failure.

## 4. Dependency cleanup check

```bash
grep -rn "requests_aws4auth\|requests-aws4auth" src/ pyproject.toml
```

**Expected**: no matches — the import is gone from
`src/pipeline/common/opensearch.py` and the dependency is removed from
`pyproject.toml`.
