"""Thin wrappers around boto3 clients."""

from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.client import BaseClient


def boto3_session(*, region_name: str = "eu-west-1", profile_name: str | None = None):
    """Create a boto3 session using profile locally and IAM role on EC2."""

    if profile_name:
        return boto3.Session(profile_name=profile_name, region_name=region_name)
    return boto3.Session(region_name=region_name)


def s3_client(
    *,
    region_name: str = "eu-west-1",
    profile_name: str | None = None,
) -> BaseClient:
    """Return an S3 client using boto3's default credential chain."""

    return boto3_session(region_name=region_name, profile_name=profile_name).client("s3")


def secrets_client(
    *,
    region_name: str = "eu-west-1",
    profile_name: str | None = None,
) -> BaseClient:
    """Return a Secrets Manager client using boto3's default credential chain."""

    return boto3_session(region_name=region_name, profile_name=profile_name).client(
        "secretsmanager"
    )


def sqs_client(
    *,
    region_name: str = "eu-west-1",
    profile_name: str | None = None,
) -> BaseClient:
    """Return an SQS client using boto3's default credential chain."""

    return boto3_session(region_name=region_name, profile_name=profile_name).client("sqs")


def generate_presigned_url(
    s3_key: str,
    *,
    bucket: str,
    expiry_seconds: int = 3600,
    region_name: str = "eu-west-1",
    profile_name: str | None = None,
) -> str | None:
    """Generate a temporary GET presigned URL for a private S3 object.

    Returns None on any error (missing credentials, non-existent key, etc.)
    so callers can degrade gracefully to a citation without a URL.
    """
    try:
        client = s3_client(region_name=region_name, profile_name=profile_name)
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": s3_key},
            ExpiresIn=expiry_seconds,
        )
    except Exception:  # noqa: BLE001
        return None


def list_s3_keys(
    prefix: str,
    *,
    bucket: str,
    region_name: str = "eu-west-1",
    profile_name: str | None = None,
    max_keys: int = 100,
) -> list[str]:
    """List object keys under ``prefix``. Returns [] on any error."""
    try:
        client = s3_client(region_name=region_name, profile_name=profile_name)
        resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=max_keys)
        return [obj["Key"] for obj in resp.get("Contents", [])]
    except Exception:  # noqa: BLE001
        return []


_NORMALIZED_SUFFIX = "__normalized.txt"
_META_SUFFIX = ".airtable_meta.json"


def resolve_original_s3_key(
    s3_key: str,
    *,
    bucket: str,
    region_name: str = "eu-west-1",
    profile_name: str | None = None,
) -> str:
    """Return the ORIGINAL document key for a chunk's indexed ``s3_key``.

    The embedding pipeline indexes the normalized-text key
    (``{dir}/{identifier}__normalized.txt``); the original attachment lives in
    the same folder as ``{dir}/{attachment_id}__{filename}``. The attachment id
    is not recoverable by string math, so we LIST the folder and pick the file
    that is neither the normalized text nor the Airtable sidecar.

    Always returns a key that exists in S3: the original when found, otherwise
    ``s3_key`` itself (which was indexed and therefore exists) — so a citation
    can never point at a missing object.
    """
    if not s3_key.endswith(_NORMALIZED_SUFFIX):
        return s3_key  # already an original (e.g. a PDF indexed directly)
    key_dir = s3_key.rsplit("/", 1)[0] + "/"
    for key in list_s3_keys(
        key_dir, bucket=bucket, region_name=region_name, profile_name=profile_name
    ):
        leaf = key.rsplit("/", 1)[-1]
        if leaf.endswith(_NORMALIZED_SUFFIX) or leaf.endswith(_META_SUFFIX):
            continue
        return key
    return s3_key


def get_secret(
    name: str,
    *,
    region_name: str = "eu-west-1",
    profile_name: str | None = None,
) -> dict[str, Any]:
    """Load a JSON secret from AWS Secrets Manager."""

    client = secrets_client(region_name=region_name, profile_name=profile_name)
    response = client.get_secret_value(SecretId=name)
    secret_string = response.get("SecretString")
    if not secret_string:
        msg = f"Secret {name!r} does not contain SecretString"
        raise ValueError(msg)
    return json.loads(secret_string)
