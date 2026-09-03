"""Shared OpenSearch client factory used by the indexer (writer) and the
retrieval module (reader).

Two connection modes:

* **Basic auth (dev / local)** — set ``username`` and ``password``. TLS is
  disabled automatically when the host is ``localhost`` or ``127.*``.
* **AWS SigV4 (prod / EC2)** — leave both empty. The factory signs requests
  with ``opensearchpy.AWSV4SignerAuth`` fed the *live*
  ``boto3.Session().get_credentials()`` object, so it re-signs each request
  from botocore's own auto-refreshing credential provider — signing never
  goes stale when the ambient IAM role's temporary credentials rotate.
"""

from __future__ import annotations

from urllib.parse import urlparse

import structlog
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

log = structlog.get_logger(__name__)


def build_opensearch_client(
    endpoint: str,
    *,
    username: str | None = None,
    password: str | None = None,
    aws_region: str = "eu-west-1",
) -> OpenSearch:
    """Build an :class:`opensearchpy.OpenSearch` client for dev or prod.

    Parameters
    ----------
    endpoint:
        Full URL, e.g.
        ``https://vpc-…eu-west-1.es.amazonaws.com`` or ``http://localhost:9200``.
    username / password:
        When both are set, basic-auth mode is used (dev / localstack).
        When either is empty, falls back to AWS SigV4 (production).
    aws_region:
        AWS region used for SigV4 signing.
    """
    parsed = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
    host = parsed.hostname or endpoint
    is_local = host in ("localhost", "127.0.0.1") or host.startswith("127.")
    port = parsed.port or (9200 if is_local else 443)
    use_ssl = not is_local
    verify_certs = use_ssl

    if username and password:
        log.info("opensearch: basic auth mode", host=host)
        return OpenSearch(
            hosts=[{"host": host, "port": port}],
            http_auth=(username, password),
            use_ssl=use_ssl,
            verify_certs=verify_certs,
            connection_class=RequestsHttpConnection,
            timeout=30,
            max_retries=3,
            retry_on_timeout=True,
        )

    # Production: AWS SigV4. Pass the *live* credentials object (not a frozen
    # snapshot) so AWSV4SignerAuth re-signs every request from botocore's own
    # auto-refreshing provider — required because IAM-role credentials are
    # temporary and expire (see specs/002-fix-opensearch-sigv4-auth).
    import boto3  # noqa: PLC0415

    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise ValueError(
            "No AWS credentials found. Configure IAM role, environment variables "
            "(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY), or ~/.aws/credentials."
        )
    aws_auth = AWSV4SignerAuth(credentials, aws_region, "es")
    log.info("opensearch: AWS SigV4 auth mode (live, auto-refreshing credentials)", host=host)
    return OpenSearch(
        hosts=[{"host": host, "port": port}],
        http_auth=aws_auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=30,
        max_retries=3,
        retry_on_timeout=True,
    )
