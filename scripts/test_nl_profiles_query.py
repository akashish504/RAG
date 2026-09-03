#!/usr/bin/env python3
"""Exercise the v2 NL search tool from the CLI (same code path as MCP).

Requires ``.env`` with ``ANTHROPIC_API_KEY``, ``AIRTABLE_PAT_TOKEN``, ``BASE_ID``,
and (for semantic mode) ``VOYAGE_API_KEY`` + a populated OpenSearch index.

Examples::

    cd /path/to/tailoredai
    python scripts/test_nl_profiles_query.py "List people whose title contains Advisor"
    python scripts/test_nl_profiles_query.py --row-count "How many employees are there in Dalberg"
    python scripts/test_nl_profiles_query.py --records-only "People whose name starts with A"
    python scripts/test_nl_profiles_query.py --source dalberg_profiles --mode airtable_only "..."
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


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)

    parser = argparse.ArgumentParser(
        description="Run the v2 `search` tool from the CLI (same path the MCP client takes)."
    )
    parser.add_argument("question", help="Natural-language question")
    parser.add_argument(
        "--source",
        default="dalberg_profiles",
        help="Single source to query (omit for default: dalberg_profiles).",
    )
    parser.add_argument(
        "--mode",
        choices=("hybrid", "semantic_only", "airtable_only"),
        default="hybrid",
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--no-answer",
        action="store_true",
        help="Skip the Claude answer-synthesis pass (faster, fewer tokens).",
    )
    parser.add_argument(
        "--row-count",
        action="store_true",
        help="Print only hit count (integer) to stdout.",
    )
    parser.add_argument(
        "--records-only",
        action="store_true",
        help="Print only the hits array (JSON).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print diagnostics / answer / markdown_table to stderr.",
    )
    args = parser.parse_args()

    if args.row_count and args.records_only:
        print("--row-count and --records-only cannot be used together", file=sys.stderr)
        sys.exit(2)

    from retrieval.mcp.tools import search_impl

    raw = search_impl(
        question=args.question,
        sources=[args.source],
        mode=args.mode,
        top_k=args.top_k,
        include_answer=not args.no_answer,
    )
    out = json.loads(raw)

    if not args.quiet:
        diag = out.get("diagnostics") or {}
        print("--- diagnostics (stderr) ---", file=sys.stderr)
        print(json.dumps(diag, indent=2, default=str), file=sys.stderr)
        print(file=sys.stderr)
        if out.get("answer"):
            print("--- answer (stderr) ---", file=sys.stderr)
            print(out["answer"], file=sys.stderr)
            print(file=sys.stderr)
        if not args.records_only and out.get("markdown_table"):
            print("--- markdown_table (stderr) ---", file=sys.stderr)
            print(out["markdown_table"], file=sys.stderr)
            print(file=sys.stderr)

    if args.row_count:
        print(len(out.get("hits") or []))
    elif args.records_only:
        print(json.dumps(out.get("hits") or [], indent=2, default=str))
    else:
        print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
