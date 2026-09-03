"""Discover Airtable bases/tables/schema and persist metadata."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pipeline.airtable_ingestion.airtable_client import AirtableClient
from pipeline.airtable_ingestion.config import (
    DEFAULT_INGESTION_CONFIG_PATH,
    load_airtable_ingestion_settings,
)
from pipeline.airtable_ingestion.s3_uploader import S3Uploader
from pipeline.airtable_ingestion.schema import save_schema_local, upload_schema_to_s3
from pipeline.common.aws import s3_client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover and store Airtable metadata")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_INGESTION_CONFIG_PATH),
        help="Path to airtable ingestion config",
    )
    parser.add_argument(
        "--upload-schema-to-s3",
        action="store_true",
        help="Force schema uploads to S3 regardless of config default",
    )
    parser.add_argument("--profile", default=None, help="Optional AWS profile")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_airtable_ingestion_settings(config_path=Path(args.config))

    airtable = AirtableClient(
        pat_token=settings.airtable_pat_token,
        timeout_seconds=settings.defaults.request_timeout_seconds,
    )
    all_bases = airtable.list_bases_with_tables()

    s3 = s3_client(region_name=settings.aws_region, profile_name=args.profile)
    uploader = S3Uploader(client=s3, bucket=settings.s3_bucket)
    should_upload = args.upload_schema_to_s3 or settings.defaults.upload_schema_to_s3

    output: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bases": all_bases,
        "tables": [],
    }

    for base in all_bases:
        base_id = base["database_id"]
        by_table_name = airtable.get_tables_with_columns(base_id=base_id)
        for table_name, payload in by_table_name.items():
            schema_payload = {
                "database_id": base_id,
                "database_name": base["database_name"],
                "table_name": table_name,
                "table_id": payload.get("table_id"),
                "schema": payload,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            save_schema_local(
                output_dir=settings.defaults.metadata_local_dir,
                database_id=base_id,
                table_name=table_name,
                schema_payload=schema_payload,
            )
            if should_upload:
                upload_schema_to_s3(
                    uploader=uploader,
                    metadata_prefix=settings.defaults.metadata_s3_prefix,
                    database_id=base_id,
                    table_name=table_name,
                    schema_payload=schema_payload,
                )
            output["tables"].append(schema_payload)

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
