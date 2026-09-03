"""Create (or verify) per-table OpenSearch indexes.

Each table has its own isolated index so datasets never share index space.
Adding a new table later requires only enabling it in tables.yaml — the
existing index for other tables is never touched.

Usage
-----
    # Create all configured table indexes:
    python scripts/create_opensearch_index.py

    # Create one specific table's index:
    python scripts/create_opensearch_index.py --table dalberg_profiles

    # Print the index mapping and exit (no cluster changes):
    python scripts/create_opensearch_index.py --show-mapping
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    from dotenv import load_dotenv  # type: ignore[import]
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from pipeline.config import DEFAULT_TABLES_PATH, load_settings
from pipeline.logging_config import setup_logging
from pipeline.embedding_pipeline.indexer.mappings import index_create_body
from pipeline.embedding_pipeline.indexer.opensearch import build_opensearch_client
from pipeline.embedding_pipeline.reader.tables import TableRegistry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create per-table OpenSearch indexes with the KNN mapping"
    )
    parser.add_argument(
        "--table",
        default=None,
        help=(
            "Create only this table's index (e.g. 'dalberg_profiles'). "
            "Omit to create all configured table indexes."
        ),
    )
    parser.add_argument(
        "--tables-config",
        default=str(DEFAULT_TABLES_PATH),
        help="Path to tables.yaml (default: config/tables.yaml).",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="AWS region override (default: AWS_REGION env var or eu-west-1).",
    )
    parser.add_argument(
        "--show-mapping",
        action="store_true",
        help="Print the index mapping JSON and exit without touching the cluster.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.show_mapping:
        # d-quals indexes get the quantized variant, so resolve the actual
        # index name when a table is given rather than printing one shared body.
        index_name = (
            TableRegistry.from_yaml(args.tables_config).get(args.table).index_name
            if args.table
            else "generic"
        )
        print(json.dumps(index_create_body(index_name), indent=2))
        return

    setup_logging(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        json_logs=os.environ.get("MCP_ENV", "dev") != "dev",
    )

    settings = load_settings()
    region = args.region or settings.aws_region
    table_registry = TableRegistry.from_yaml(args.tables_config)

    client = build_opensearch_client(
        settings.opensearch.endpoint,
        username=settings.opensearch.username,
        password=settings.opensearch.password,
        aws_region=region,
    )

    # Determine which tables to create indexes for.
    if args.table:
        tables = [table_registry.get(args.table)]
    else:
        tables = table_registry.list()

    created = 0
    skipped = 0
    for table in tables:
        idx = table.index_name
        if client.indices.exists(index=idx):
            print(f"  EXISTS   '{idx}'  (table: {table.name})")
            skipped += 1
        else:
            client.indices.create(
                index=idx,
                body=index_create_body(idx),
            )
            print(f"  CREATED  '{idx}'  (table: {table.name})")
            created += 1

    print(f"\nDone — {created} created, {skipped} already existed.")


if __name__ == "__main__":
    main()
