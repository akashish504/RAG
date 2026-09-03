"""Read one S3 object, parse it, and chunk it. Print a summary.

Examples:
    python scripts/chunk_s3_object.py --bucket claude-mcp-object-store \\
        --key raw/profile/recABC/cv.txt

    python scripts/chunk_s3_object.py --bucket claude-mcp-object-store \\
        --key raw/proposals/recXYZ/proposal.txt --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pipeline.common.aws import s3_client
from pipeline.config import DEFAULT_TABLES_PATH
from pipeline.embedding_pipeline.chunker.registry import default_chunker_registry
from pipeline.embedding_pipeline.parser.registry import ParserRegistry
from pipeline.embedding_pipeline.reader.document_loader import DocumentLoader
from pipeline.embedding_pipeline.reader.s3_reader import S3Reader
from pipeline.embedding_pipeline.reader.tables import TableRegistry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read, parse, and chunk one S3 object")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--tables-config", default=str(DEFAULT_TABLES_PATH))
    parser.add_argument(
        "--strategy",
        default=None,
        help="Override the chunker strategy. Defaults to the table's configured strategy.",
    )
    parser.add_argument("--json", action="store_true", help="Print chunks as JSON")
    parser.add_argument("--limit", type=int, default=5, help="Show at most N chunks")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    client = s3_client(region_name=args.region, profile_name=args.profile)
    reader = S3Reader(bucket=args.bucket, s3_client=client)
    parser_registry = ParserRegistry()
    table_registry = TableRegistry.from_yaml(args.tables_config)
    chunker_registry = default_chunker_registry()

    loader = DocumentLoader(
        reader=reader,
        parser_registry=parser_registry,
        table_registry=table_registry,
    )
    loaded = loader.load_one(args.key)
    strategy_name = args.strategy or loaded.table.chunker_strategy
    chunker = chunker_registry.get(strategy_name)
    chunks = chunker.chunk(
        document=loaded.document,
        parsed=loaded.parsed,
        config=loaded.table.chunking,
    )

    summary = {
        "table": loaded.table.name,
        "strategy": strategy_name,
        "s3_path": loaded.document.source.s3_key,
        "document_id": loaded.document.source.primary_key,
        "document_hash": loaded.document.document_hash,
        "parser": loaded.parsed.parser_name,
        "section_count": len(loaded.parsed.sections),
        "chunk_count": len(chunks),
        "parent_chunks": sum(1 for c in chunks if c.chunk_type.value == "parent"),
        "child_chunks": sum(1 for c in chunks if c.chunk_type.value == "child"),
    }

    if args.json:
        printable = {
            "summary": summary,
            "chunks": [
                {
                    "chunk_id": c.chunk_id,
                    "chunk_type": c.chunk_type.value,
                    "parent_chunk_id": c.parent_chunk_id,
                    "token_count": c.token_count,
                    "chunk_index": c.chunk_index,
                    "metadata": c.metadata,
                    "text_preview": c.text[:200],
                }
                for c in chunks[: args.limit]
            ],
        }
        print(json.dumps(printable, indent=2, default=str))
        return

    print("Summary")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    print()
    for chunk in chunks[: args.limit]:
        print(
            f"[{chunk.chunk_type.value}] id={chunk.chunk_id} "
            f"tokens={chunk.token_count} idx={chunk.chunk_index} "
            f"section={chunk.metadata.get('section_title')!r}"
        )
        print(f"    {chunk.text[:160].strip()}")
        print()


if __name__ == "__main__":
    main()
