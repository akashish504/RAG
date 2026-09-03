# Phase 1 Contract: `build_opensearch_client`

This is an internal factory function, not a network-facing endpoint — this
is the one "interface" this feature touches, since it's the shared contract
both internal call sites (`pipeline/embedding_pipeline/indexer/opensearch.py`,
`retrieval/sources/opensearch.py`) rely on.

## Signature (unchanged)

```python
def build_opensearch_client(
    endpoint: str,
    *,
    username: str | None = None,
    password: str | None = None,
    aws_region: str = "eu-west-1",
) -> OpenSearch
```

No signature, return type, or call-site change. Both consumers keep calling
this exactly as they do today.

## Behavior contract

**Given** `username` and `password` are both set:
- **Then** returns an `OpenSearch` client using HTTP basic auth, TLS
  disabled only for `localhost`/`127.*` hosts. (Unchanged.)

**Given** `username`/`password` are not both set (production / IAM-role path):
- **Then** returns an `OpenSearch` client authenticated via AWS SigV4, where
  the signer holds a reference to the **live** boto3 credentials object
  (`boto3.Session().get_credentials()`), not a frozen snapshot.
- **And** every request signed by this client — for the entire lifetime of
  the process, across any number of underlying IAM credential rotations —
  authenticates successfully, without requiring the client (or process) to
  be rebuilt or restarted.
- **And** if no AWS credentials can be resolved at all (no role, no static
  keys, no profile), raises `ValueError` with a clear message, at
  construction time — same as today.

## Non-functional contract

- No additional network call is introduced on the per-request signing path
  under normal conditions (credentials well within their validity window).
- No change to TLS/certificate verification behavior for either auth mode.
- `requests-aws4auth` is no longer imported by this module once the fix
  lands (removed from `pyproject.toml` dependencies in the same change).
