"""S3 upload helper for Airtable ingestion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from botocore.client import BaseClient
from botocore.exceptions import ClientError


class S3Uploader:
    """Reusable S3 uploader for bytes/files/json payloads."""

    def __init__(self, *, client: BaseClient, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def key_exists(self, *, key: str) -> bool:
        """Return True if the S3 key already exists."""
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def get_etag(self, *, key: str) -> str | None:
        """Return the ETag (MD5 for single-part uploads) of an existing key, or None."""
        try:
            resp = self.client.head_object(Bucket=self.bucket, Key=key)
            return resp["ETag"].strip('"')
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return None
            raise

    def get_text(self, *, key: str) -> str | None:
        """Return the UTF-8 body of an existing key, or None if it does not exist.

        Used to read back an already-processed ``normalized.txt`` on re-runs so a
        record-level summary can still aggregate files that were skipped this pass.
        """
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return None
            raise
        return resp["Body"].read().decode("utf-8", errors="replace")

    def get_bytes(self, *, key: str) -> bytes | None:
        """Return the raw bytes of an existing key, or None if it does not exist.

        Lets the sync pipeline reuse an original already in S3 instead of
        re-downloading it from Airtable on a re-run.
        """
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return None
            raise
        return resp["Body"].read()

    def upload_file(self, *, local_path: str | Path, key: str, content_type: str | None = None) -> None:
        extra: dict[str, str] = {}
        if content_type:
            extra["ContentType"] = content_type
        self.client.upload_file(
            str(local_path),
            self.bucket,
            key,
            ExtraArgs=extra or None,
        )

    def upload_bytes(self, *, data: bytes, key: str, content_type: str | None = None) -> None:
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": key, "Body": data}
        if content_type:
            kwargs["ContentType"] = content_type
        self.client.put_object(**kwargs)

    def upload_json(self, *, payload: dict[str, Any] | list[Any], key: str) -> None:
        blob = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.upload_bytes(data=blob, key=key, content_type="application/json")

    def delete_object(self, *, key: str) -> None:
        """Delete an S3 object. No-op (does not raise) if it doesn't exist."""
        self.client.delete_object(Bucket=self.bucket, Key=key)
