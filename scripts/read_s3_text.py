"""Fetch one .txt object from S3 and print a reader smoke-test summary.

Example:
    python scripts/read_s3_text.py \
      --bucket dalberg-mcp-documents \
      --key raw/contracts/recABC123/proposal_text.txt

For early testing, a flat key such as sample.txt is also supported:
    python scripts/read_s3_text.py --bucket <bucket> --key sample.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pipeline.common.aws import s3_client
from pipeline.embedding_pipeline.reader.s3_reader import S3Reader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read one .txt object from S3")
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument("--key", required=True, help="S3 object key, e.g. sample.txt")
    parser.add_argument("--region", default="eu-west-1", help="AWS region")
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional AWS profile for local use. Omit on EC2 to use instance role.",
    )
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=500,
        help="Number of text characters to print as preview",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = s3_client(region_name=args.region, profile_name=args.profile)
    reader = S3Reader(bucket=args.bucket, s3_client=client)
    document = reader.get_document(args.key)

    source = document.source
    print("S3 read successful")
    print(f"source_url: {source.source_url}")
    print(f"document_hash: {document.document_hash}")
    print(f"content_length: {document.content_length}")
    print(f"table_name: {source.table_name}")
    print(f"primary_key: {source.primary_key}")
    print(f"column_name: {source.column_name}")
    print("")
    print("Preview:")
    print(document.text[: args.preview_chars])


if __name__ == "__main__":
    main()
