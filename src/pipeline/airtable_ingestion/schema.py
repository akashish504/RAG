"""Schema discovery and persistence helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.airtable_ingestion.normalizers import slugify_table_name
from pipeline.airtable_ingestion.s3_uploader import S3Uploader


def save_schema_local(
    *,
    output_dir: str | Path,
    database_id: str,
    table_name: str,
    schema_payload: dict[str, Any],
) -> Path:
    """Persist schema JSON locally."""

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{database_id}__{slugify_table_name(table_name)}.json"
    path = out_dir / file_name
    path.write_text(json.dumps(schema_payload, indent=2, ensure_ascii=False))
    return path


def schema_s3_key(*, metadata_prefix: str, database_id: str, table_name: str) -> str:
    table_slug = slugify_table_name(table_name)
    return f"{metadata_prefix.rstrip('/')}/{database_id}/{table_slug}/schema.json"


def upload_schema_to_s3(
    *,
    uploader: S3Uploader,
    metadata_prefix: str,
    database_id: str,
    table_name: str,
    schema_payload: dict[str, Any],
) -> str:
    key = schema_s3_key(
        metadata_prefix=metadata_prefix,
        database_id=database_id,
        table_name=table_name,
    )
    uploader.upload_json(payload=schema_payload, key=key)
    return key
