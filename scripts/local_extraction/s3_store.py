"""Thin S3 adapter — get, put, list, json helpers."""

from __future__ import annotations

import json


class S3Store:
    def __init__(self, *, client, bucket: str) -> None:
        self._c = client
        self._bucket = bucket

    def iter_keys(self, prefix: str):
        paginator = self._c.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                yield obj["Key"]

    def get_bytes(self, key: str) -> bytes:
        return self._c.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def get_json(self, key: str) -> dict:
        try:
            return json.loads(self.get_bytes(key).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def put_text(self, key: str, text: str) -> None:
        self._c.put_object(
            Bucket=self._bucket, Key=key,
            Body=text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )

    def put_json(self, key: str, payload: dict) -> None:
        self._c.put_object(
            Bucket=self._bucket, Key=key,
            Body=json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
