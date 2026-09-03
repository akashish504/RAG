"""Read-only validation tool for indexed embedding data.

Four modes:

A) Summary — overall stats for a table's index
   python scripts/validate_opensearch.py --table dalberg_profiles

B) File check — per-document breakdown for one S3 key
   python scripts/validate_opensearch.py \\
       --s3-key "raw/dalberg_profiles/john@example.com/cv_attachment/attXXX__cv.pdf"

C) Profile-scoped KNN test — embed a query and search (requires VOYAGE_API_KEY)
   python scripts/validate_opensearch.py \\
       --test-query "machine learning engineer" \\
       --table dalberg_profiles \\
       --primary-key "john@example.com"   # optional: scope to one profile

D) Raw chunks — print every chunk for a file
   python scripts/validate_opensearch.py \\
       --s3-key "raw/dalberg_profiles/..." --show-chunks
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

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
from pipeline.embedding_pipeline.indexer.opensearch import build_opensearch_client
from pipeline.embedding_pipeline.reader.tables import TableRegistry


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only validation of OpenSearch indexed data"
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--table",
        default=None,
        help="Table name to summarise (e.g. 'dalberg_profiles').",
    )
    mode.add_argument(
        "--s3-key",
        default=None,
        help="S3 key to inspect (file-level breakdown or raw chunks).",
    )
    mode.add_argument(
        "--test-query",
        default=None,
        help="Text to embed and search with KNN (requires VOYAGE_API_KEY).",
    )

    parser.add_argument(
        "--primary-key",
        default=None,
        help="Filter KNN results to one profile (email). Used with --test-query.",
    )
    parser.add_argument(
        "--search-table",
        default=None,
        dest="search_table",
        help="Table to search when using --test-query (e.g. 'dalberg_profiles').",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of KNN hits to return (default: 5).",
    )
    parser.add_argument(
        "--show-chunks",
        action="store_true",
        help="Print raw chunk text when using --s3-key.",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="Disable parent+person dedup for --test-query (shows raw hits).",
    )
    parser.add_argument(
        "--hybrid",
        action="store_true",
        help="Use hybrid BM25+KNN search with RRF instead of pure KNN (default: KNN only).",
    )
    parser.add_argument(
        "--tables-config",
        default=str(DEFAULT_TABLES_PATH),
        help="Path to tables.yaml (default: config/tables.yaml).",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="AWS region override.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Mode A — Table summary
# ---------------------------------------------------------------------------


def mode_summary(client: Any, index: str, table_name: str) -> None:
    """Print overall stats for a table's index."""

    # Total doc count
    count_resp = client.count(index=index, body={"query": {"match_all": {}}})
    total_chunks = count_resp.get("count", 0)

    if total_chunks == 0:
        print(f"\nIndex '{index}' is empty — no chunks indexed yet.")
        return

    # Aggregations: unique files, chunk_type split, embedding coverage, model, timestamps
    agg_body = {
        "size": 0,
        "aggs": {
            "unique_files": {"cardinality": {"field": "s3_key"}},
            "by_chunk_type": {
                "terms": {"field": "chunk_type"},
                "aggs": {
                    "embedded_count": {
                        "filter": {"exists": {"field": "embedding"}}
                    }
                },
            },
            "by_model": {"terms": {"field": "embedding_model", "size": 10}},
            "oldest": {"min": {"field": "indexed_at"}},
            "newest": {"max": {"field": "indexed_at"}},
        },
    }
    agg_resp = client.search(index=index, body=agg_body)
    aggs = agg_resp.get("aggregations", {})

    unique_files = aggs.get("unique_files", {}).get("value", 0)

    parents = 0
    children = 0
    children_embedded = 0
    for bucket in aggs.get("by_chunk_type", {}).get("buckets", []):
        ct = bucket["key"]
        cnt = bucket["doc_count"]
        embedded = bucket.get("embedded_count", {}).get("doc_count", 0)
        if ct == "parent":
            parents = cnt
        elif ct == "child":
            children = cnt
            children_embedded = embedded

    models = {
        b["key"]: b["doc_count"]
        for b in aggs.get("by_model", {}).get("buckets", [])
    }
    model_str = ", ".join(f"{m} ({c})" for m, c in models.items()) or "n/a"

    oldest = aggs.get("oldest", {}).get("value_as_string", "n/a")
    newest = aggs.get("newest", {}).get("value_as_string", "n/a")
    embed_pct = f"{children_embedded}/{children}" if children else "0/0"

    print(f"\nIndex: {index}  (table: {table_name})")
    print(f"  Unique files indexed  : {unique_files}")
    print(f"  Parent chunks total   : {parents}")
    print(f"  Child chunks total    : {children}")
    print(f"  Children with vectors : {embed_pct}")
    print(f"  Embedding models      : {model_str}")
    print(f"  Oldest indexed_at     : {oldest}")
    print(f"  Newest indexed_at     : {newest}")


# ---------------------------------------------------------------------------
# Mode B — File-level check
# ---------------------------------------------------------------------------


def mode_file_check(client: Any, index: str, s3_key: str, *, show_chunks: bool) -> None:
    """Print per-document breakdown for one S3 key."""

    resp = client.search(
        index=index,
        body={
            "query": {"term": {"s3_key": s3_key}},
            "size": 500,
            "_source": [
                "chunk_id", "chunk_type", "parent_chunk_id",
                "primary_key", "document_hash", "indexed_at",
                "token_count", "text",
                "metadata.section_canonical", "metadata.entry_index",
                "metadata.child_index_in_parent", "metadata.section_title",
            ],
            "sort": [{"chunk_type": "asc"}, {"position": "asc"}],
        },
    )

    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        print(f"\nNo chunks found for s3_key: {s3_key}")
        print(f"(searched index: {index})")
        return

    sources = [h["_source"] for h in hits]
    parents = [s for s in sources if s.get("chunk_type") == "parent"]
    children = [s for s in sources if s.get("chunk_type") == "child"]

    # knn_vector fields are not returned in _source — use an aggregation to
    # count children whose embedding field actually exists in the index.
    agg_resp = client.search(
        index=index,
        body={
            "size": 0,
            "query": {
                "bool": {
                    "must": [
                        {"term": {"s3_key": s3_key}},
                        {"term": {"chunk_type": "child"}},
                    ]
                }
            },
            "aggs": {
                "embedded": {"filter": {"exists": {"field": "embedding"}}}
            },
        },
    )
    children_embedded = (
        agg_resp.get("aggregations", {}).get("embedded", {}).get("doc_count", 0)
    )

    first = sources[0]
    print(f"\nFile  : {s3_key}")
    print(f"  primary_key  : {first.get('primary_key', 'n/a')}")
    print(f"  document_hash: {first.get('document_hash', 'n/a')[:16]}...")
    print(f"  indexed_at   : {first.get('indexed_at', 'n/a')}")
    print(f"  Parents : {len(parents)}")
    print(f"  Children: {len(children)}  embedded: {children_embedded}/{len(children)}")

    # Section breakdown
    from collections import defaultdict
    by_section: dict[str, int] = defaultdict(int)
    for c in children:
        sec = (c.get("metadata") or {}).get("section_canonical", "unknown")
        by_section[sec] += 1

    if by_section:
        print("\n  Children per section:")
        for sec, cnt in sorted(by_section.items(), key=lambda x: -x[1]):
            print(f"    {sec:<20}: {cnt}")

    if show_chunks:
        print("\n  ── Raw chunks ──────────────────────────────────────────")
        for s in sources:
            meta = s.get("metadata") or {}
            print(
                f"\n  [{s.get('chunk_type','?')}] id={s.get('chunk_id','')[:24]}..."
                f"  section={meta.get('section_canonical','?')}"
                f"  tokens={s.get('token_count','?')}"
            )
            if s.get("parent_chunk_id"):
                print(f"         parent_chunk_id={s['parent_chunk_id'][:24]}...")
            text_preview = (s.get("text") or "")[:200].replace("\n", " ")
            print(f"         text: {text_preview}{'...' if len(s.get('text',''))>200 else ''}")


# ---------------------------------------------------------------------------
# Mode C — Profile-scoped KNN test
# ---------------------------------------------------------------------------


def mode_knn_test(
    client: Any,
    index: str,
    query_text: str,
    *,
    primary_key: str | None,
    top_k: int,
    table_name: str | None,
    dedup: bool = True,
) -> None:
    """Embed a query text and run a KNN search, optionally scoped to one profile."""

    voyage_key = os.environ.get("VOYAGE_API_KEY", "").strip()
    if not voyage_key:
        print("ERROR: VOYAGE_API_KEY is not set. Cannot embed query.")
        sys.exit(1)

    try:
        import voyageai  # type: ignore[import]
    except ImportError:
        print("ERROR: voyageai not installed. Run: pip install 'dalberg-mcp[voyage]'")
        sys.exit(1)

    vo_client = voyageai.Client(api_key=voyage_key)
    voyage_model = os.environ.get("VOYAGE_MODEL", "voyage-4")

    print(f"\nEmbedding query with {voyage_model}: '{query_text}'")
    result = vo_client.embed([f"query: {query_text}"], model=voyage_model)
    query_vector = result.embeddings[0]

    # Over-fetch before dedup so we still have top_k after collapsing duplicates.
    fetch_size = max(top_k * 5, 50) if dedup else top_k

    # Build KNN query with optional filters.
    filters: list[dict] = [{"term": {"chunk_type": "child"}}]
    if table_name:
        filters.append({"term": {"table_name": table_name}})
    if primary_key:
        filters.append({"term": {"primary_key": primary_key}})

    knn_body: dict = {
        "size": fetch_size,
        "_source": [
            "chunk_id", "primary_key", "s3_key", "parent_chunk_id",
            "token_count", "text",
            "metadata.section_canonical", "metadata.entry_index",
        ],
        "query": {
            "knn": {
                "embedding": {
                    "vector": query_vector,
                    "k": fetch_size,
                    "filter": {"bool": {"must": filters}},
                }
            }
        },
    }

    scope = f"profile={primary_key}" if primary_key else (table_name or index)
    print(f"KNN query: '{query_text}'  [scope: {scope}]")

    resp = client.search(index=index, body=knn_body)
    hits = resp.get("hits", {}).get("hits", [])

    if not hits:
        print("  No results found.")
        return

    raw_count = len(hits)

    if dedup:
        # Parent-level dedup: keep best child per parent chunk.
        parent_best: dict[str, dict] = {}
        for hit in hits:
            pid = hit["_source"].get("parent_chunk_id") or hit["_id"]
            if pid not in parent_best or hit["_score"] > parent_best[pid]["_score"]:
                parent_best[pid] = hit
        hits = sorted(parent_best.values(), key=lambda h: h["_score"], reverse=True)

        # Person-level dedup: keep best result per primary_key.
        person_best: dict[str, dict] = {}
        for hit in hits:
            pk = hit["_source"].get("primary_key") or hit["_id"]
            if pk not in person_best or hit["_score"] > person_best[pk]["_score"]:
                person_best[pk] = hit
        hits = sorted(person_best.values(), key=lambda h: h["_score"], reverse=True)[:top_k]

        print(f"  [DEDUPED {raw_count}→{len(hits)}]  (parent+person dedup; --no-dedup to disable)")

    for i, hit in enumerate(hits, 1):
        src = hit["_source"]
        meta = src.get("metadata") or {}
        text_preview = (src.get("text") or "")[:150].replace("\n", " ")
        print(
            f"\n  Hit {i}: score={hit['_score']:.4f}"
            f"  section={meta.get('section_canonical', '?')}"
            f"  primary_key={src.get('primary_key', '?')}"
        )
        print(f"          s3_key={src.get('s3_key', '?')}")
        print(f"          text: {text_preview}{'...' if len(src.get('text',''))>150 else ''}")


# ---------------------------------------------------------------------------
# Mode C2 — Hybrid BM25 + KNN search with RRF
# ---------------------------------------------------------------------------


def mode_hybrid_test(
    client: Any,
    index: str,
    query_text: str,
    query_vector: list,
    *,
    primary_key: str | None,
    top_k: int,
    table_name: str | None,
    dedup: bool = True,
    rrf_k: int = 60,
) -> None:
    """Run BM25 and KNN independently then fuse results with Reciprocal Rank Fusion."""

    fetch_size = max(top_k * 5, 50) if dedup else top_k * 2

    source_fields = [
        "chunk_id", "primary_key", "s3_key", "parent_chunk_id",
        "token_count", "text",
        "metadata.section_canonical", "metadata.entry_index",
    ]
    filters: list[dict] = [{"term": {"chunk_type": "child"}}]
    if table_name:
        filters.append({"term": {"table_name": table_name}})
    if primary_key:
        filters.append({"term": {"primary_key": primary_key}})

    bm25_body: dict = {
        "size": fetch_size,
        "_source": source_fields,
        "query": {
            "bool": {
                "must": {"match": {"text": query_text}},
                "filter": filters,
            }
        },
    }
    knn_body: dict = {
        "size": fetch_size,
        "_source": source_fields,
        "query": {
            "knn": {
                "embedding": {
                    "vector": query_vector,
                    "k": fetch_size,
                    "filter": {"bool": {"must": filters}},
                }
            }
        },
    }

    bm25_hits = client.search(index=index, body=bm25_body).get("hits", {}).get("hits", [])
    knn_hits = client.search(index=index, body=knn_body).get("hits", {}).get("hits", [])

    print(f"  BM25: {len(bm25_hits)} hits   KNN: {len(knn_hits)} hits  →  fusing with RRF(k={rrf_k})")

    # Reciprocal Rank Fusion: score = Σ 1/(k + rank) across both result lists.
    rrf_scores: dict[str, float] = {}
    hit_by_id: dict[str, dict] = {}
    for rank, hit in enumerate(bm25_hits, 1):
        rrf_scores[hit["_id"]] = rrf_scores.get(hit["_id"], 0.0) + 1.0 / (rrf_k + rank)
        hit_by_id[hit["_id"]] = hit
    for rank, hit in enumerate(knn_hits, 1):
        rrf_scores[hit["_id"]] = rrf_scores.get(hit["_id"], 0.0) + 1.0 / (rrf_k + rank)
        hit_by_id[hit["_id"]] = hit

    hits = [
        {**hit_by_id[doc_id], "_score": rrf_score}
        for doc_id, rrf_score in sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    ]
    raw_count = len(hits)

    if dedup:
        parent_best: dict[str, dict] = {}
        for hit in hits:
            pid = hit["_source"].get("parent_chunk_id") or hit["_id"]
            if pid not in parent_best or hit["_score"] > parent_best[pid]["_score"]:
                parent_best[pid] = hit
        hits = sorted(parent_best.values(), key=lambda h: h["_score"], reverse=True)

        person_best: dict[str, dict] = {}
        for hit in hits:
            pk = hit["_source"].get("primary_key") or hit["_id"]
            if pk not in person_best or hit["_score"] > person_best[pk]["_score"]:
                person_best[pk] = hit
        hits = sorted(person_best.values(), key=lambda h: h["_score"], reverse=True)[:top_k]
        print(f"  [DEDUPED {raw_count}→{len(hits)}]  (parent+person dedup; --no-dedup to disable)")
    else:
        hits = hits[:top_k]

    for i, hit in enumerate(hits, 1):
        src = hit["_source"]
        meta = src.get("metadata") or {}
        text_preview = (src.get("text") or "")[:150].replace("\n", " ")
        print(
            f"\n  Hit {i}: rrf_score={hit['_score']:.4f}"
            f"  section={meta.get('section_canonical', '?')}"
            f"  primary_key={src.get('primary_key', '?')}"
        )
        print(f"          s3_key={src.get('s3_key', '?')}")
        print(f"          text: {text_preview}{'...' if len(src.get('text',''))>150 else ''}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()

    settings = load_settings()
    region = args.region or settings.aws_region
    table_registry = TableRegistry.from_yaml(args.tables_config)

    client = build_opensearch_client(
        settings.opensearch.endpoint,
        username=settings.opensearch.username,
        password=settings.opensearch.password,
        aws_region=region,
    )

    if args.table:
        table = table_registry.get(args.table)
        mode_summary(client, index=table.index_name, table_name=table.name)

    elif args.s3_key:
        # Resolve index from the S3 key's table prefix.
        table = table_registry.resolve_from_key(args.s3_key)
        if table is None:
            print(f"WARNING: s3_key '{args.s3_key}' does not match any configured table.")
            print("Searching all managed indexes...")
            all_indexes = ",".join(t.index_name for t in table_registry.list())
            index = all_indexes
        else:
            index = table.index_name
        mode_file_check(client, index=index, s3_key=args.s3_key, show_chunks=args.show_chunks)

    elif args.test_query:
        # Determine which table/index to search.
        search_table_name = args.search_table
        if search_table_name:
            table = table_registry.get(search_table_name)
            index = table.index_name
        else:
            index = ",".join(t.index_name for t in table_registry.list())
            search_table_name = None

        if args.hybrid:
            # Embed once, then run hybrid BM25+KNN with RRF.
            voyage_key = os.environ.get("VOYAGE_API_KEY", "").strip()
            if not voyage_key:
                print("ERROR: VOYAGE_API_KEY is not set.")
                sys.exit(1)
            try:
                import voyageai  # type: ignore[import]
            except ImportError:
                print("ERROR: voyageai not installed.")
                sys.exit(1)
            voyage_model = os.environ.get("VOYAGE_MODEL", "voyage-4")
            print(f"\nEmbedding query with {voyage_model}: '{args.test_query}'")
            query_vector = voyageai.Client(api_key=voyage_key).embed(
                [f"query: {args.test_query}"], model=voyage_model
            ).embeddings[0]
            print(f"Hybrid BM25+KNN query: '{args.test_query}'  [scope: {search_table_name or index}]")
            mode_hybrid_test(
                client,
                index=index,
                query_text=args.test_query,
                query_vector=query_vector,
                primary_key=args.primary_key,
                top_k=args.top_k,
                table_name=search_table_name,
                dedup=not args.no_dedup,
            )
        else:
            mode_knn_test(
                client,
                index=index,
                query_text=args.test_query,
                primary_key=args.primary_key,
                top_k=args.top_k,
                table_name=search_table_name,
                dedup=not args.no_dedup,
            )


if __name__ == "__main__":
    main()
