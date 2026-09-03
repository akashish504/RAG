"""One-time migration: ``mcp-d-quals`` → ``mcp-d-quals-v2`` (faiss 32x on-disk).

Spec: specs/006-dquals-faiss-quantization/spec.md

Reindexes the D.Quals index into a binary-quantized mapping so the resident
HNSW graph (~6 GB) shrinks to ~360 MB and fits t3.medium's page cache. Run
each step on the EC2 host while the domain is temporarily scaled up:

    docker compose run --rm pipeline python scripts/migrate_dquals_v2.py check
    docker compose run --rm pipeline python scripts/migrate_dquals_v2.py create
    docker compose run --rm pipeline python scripts/migrate_dquals_v2.py benchmark
    docker compose run --rm pipeline python scripts/migrate_dquals_v2.py reindex
    docker compose run --rm pipeline python scripts/migrate_dquals_v2.py status <task_id>
    docker compose run --rm pipeline python scripts/migrate_dquals_v2.py delta      # catch docs ingested mid-copy
    docker compose run --rm pipeline python scripts/migrate_dquals_v2.py finalize
    docker compose run --rm pipeline python scripts/migrate_dquals_v2.py verify

Gates (do not proceed past a failed step):
  check     node RAM ≈ 128 GB (r7g.4xlarge), merges drained, disk headroom
  status    complete = task done AND merges.current == 0 (task "completed"
            alone only means the copy finished — graph merges continue after)
  verify    doc-count parity + child-only kNN + old/new top-k overlap,
            while BOTH indexes still exist

The source index is never modified. Rollback before deletion is simply:
keep serving mcp-d-quals (no config change).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
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

from pipeline.common.opensearch import build_opensearch_client  # noqa: E402
from pipeline.embedding_pipeline.indexer.mappings import index_create_body  # noqa: E402

SOURCE = "mcp-d-quals"
# Default target; override with --target (e.g. mcp-d-quals-16x for the 16x
# evaluation candidate — its level comes from QUANTIZED_LEVEL_OVERRIDES).
TARGET = "mcp-d-quals-v2"


def _expected_level() -> str:
    """Compression level the mapping definition prescribes for TARGET."""
    return index_create_body(TARGET)["mappings"]["properties"]["embedding"]["compression_level"]

# Build-time index settings — applied at create, partially reverted by
# `finalize`. Replicas stay 0 permanently (single-node domain).
BUILD_SETTINGS = {
    "number_of_replicas": 0,
    "refresh_interval": "-1",
    "knn.algo_param.ef_search": 512,
}
SERVE_REFRESH_INTERVAL = "30s"

REINDEX_SLICES = 4
BENCHMARK_DOCS = 50_000
# Sync calls that do real server-side work (benchmark reindex) need far more
# than the client's default 30 s.
LONG_TIMEOUT = 7_200


def _client():
    endpoint = os.environ.get("OPENSEARCH_ENDPOINT")
    if not endpoint:
        sys.exit("OPENSEARCH_ENDPOINT is not set")
    return build_opensearch_client(endpoint)


def _reindex_body(max_docs: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source": {"index": SOURCE, "size": 1000},
        # op_type create + conflicts proceed: already-copied docs are skipped,
        # so benchmark/reindex/delta are all safely re-runnable passes.
        "dest": {"index": TARGET, "op_type": "create"},
        "conflicts": "proceed",
    }
    if max_docs is not None:
        body["max_docs"] = max_docs
    return body


def _print_node_health(c) -> int:
    print(c.cat.nodes(v=True, h="name,cpu,load_1m,heap.percent,heap.max,ram.max,merges.current"))
    print(c.cat.allocation(v=True))
    merges = sum(
        int(line.split()[-1])
        for line in c.cat.nodes(h="name,merges.current").strip().splitlines()
        if line.split()
    )
    return merges


def cmd_check(c) -> None:
    merges = _print_node_health(c)
    print(c.cat.health(v=True))
    print(c.cat.indices(index=f"{SOURCE}*", v=True))

    node = c.nodes.stats(metric="os,jvm")["nodes"]
    mem_gb = max(n["os"]["mem"]["total_in_bytes"] for n in node.values()) / 2**30
    total = c.count(index=SOURCE)["count"]
    children = c.count(
        index=SOURCE, body={"query": {"term": {"chunk_type": "child"}}}
    )["count"]
    print(f"\nnode RAM: {mem_gb:.0f} GiB | source docs: {total:,} ({children:,} children)")

    ok = True
    if mem_gb < 100:
        ok = False
        print("FAIL: node RAM < 100 GiB — this is not the scaled-up r7g.4xlarge. Do not proceed.")
    if merges > 0:
        ok = False
        print(f"FAIL: merges.current = {merges} — wait for background merges to drain.")
    print("check: OK — proceed to `create`" if ok else "check: FAILED")
    sys.exit(0 if ok else 1)


def cmd_create(c, recreate: bool) -> None:
    if c.indices.exists(index=TARGET):
        if not recreate:
            sys.exit(f"{TARGET} already exists. Pass --recreate to drop and start over.")
        print(f"deleting existing {TARGET} …")
        c.indices.delete(index=TARGET)

    body = index_create_body(TARGET)
    body["settings"]["index"].update(BUILD_SETTINGS)
    c.indices.create(index=TARGET, body=body)

    emb = c.indices.get_mapping(index=TARGET)[TARGET]["mappings"]["properties"]["embedding"]
    print(f"created {TARGET}")
    print(f"embedding mapping: {emb}")
    if emb.get("mode") != "on_disk" or emb.get("compression_level") != _expected_level():
        sys.exit(
            f"FAIL: mapping is not quantized at {_expected_level()} — "
            "investigate before reindexing."
        )
    print("create: OK — proceed to `benchmark`")


def cmd_benchmark(c) -> None:
    t0 = time.monotonic()
    resp = c.reindex(
        body=_reindex_body(max_docs=BENCHMARK_DOCS),
        wait_for_completion=True,
        slices=REINDEX_SLICES,
        requests_per_second=-1,
        request_timeout=LONG_TIMEOUT,
    )
    elapsed = time.monotonic() - t0

    created = resp.get("created", 0)
    total_source = c.count(index=SOURCE)["count"]
    rate = created / elapsed if elapsed else 0
    remaining = total_source - created
    est_min = remaining / rate / 60 if rate else float("inf")
    print(f"copied {created:,} docs in {elapsed:.0f}s ({rate:.0f} docs/s)")
    print(f"extrapolated full run for remaining {remaining:,} docs: ~{est_min:.0f} min")
    print(f"failures: {resp.get('failures') or 'none'}")
    _print_node_health(c)
    print(
        "benchmark: done — these docs count toward the real run (create-only copy).\n"
        "If the estimate is acceptable, proceed to `reindex`."
    )


def cmd_reindex(c) -> None:
    resp = c.reindex(
        body=_reindex_body(),
        wait_for_completion=False,
        slices=REINDEX_SLICES,
        requests_per_second=-1,
    )
    task_id = resp["task"]
    print(f"reindex started, task: {task_id}")
    print(f"poll with:   … migrate_dquals_v2.py status {task_id}")
    print(f"cancel with: POST _tasks/{task_id}/_cancel  (source index is never at risk)")


def cmd_status(c, task_id: str) -> None:
    task = c.tasks.get(task_id=task_id)
    st = task["task"]["status"]
    done = st.get("created", 0) + st.get("updated", 0) + st.get("deleted", 0)
    skipped = st.get("noops", 0) + len(st.get("failures", []) or [])
    print(f"task completed: {task['completed']} | {done:,}/{st.get('total', 0):,} copied "
          f"(+{skipped:,} skipped/noop)")
    merges = _print_node_health(c)
    if task["completed"] and merges == 0:
        print("status: DONE — copy finished and merges drained. Run `delta`, then `finalize`.")
    elif task["completed"]:
        print("status: copy task finished but merges still running — NOT done yet, keep polling.")
    else:
        print("status: still copying.")


def cmd_delta(c) -> None:
    # Docs ingested into the source while the main copy ran are picked up
    # here; everything already copied is skipped by op_type=create.
    resp = c.reindex(
        body=_reindex_body(),
        wait_for_completion=True,
        slices=REINDEX_SLICES,
        requests_per_second=-1,
        request_timeout=LONG_TIMEOUT,
    )
    print(f"delta pass: created {resp.get('created', 0):,}, "
          f"skipped {resp.get('noops', 0):,} already-present docs")


def cmd_finalize(c) -> None:
    c.indices.put_settings(
        index=TARGET, body={"index": {"refresh_interval": SERVE_REFRESH_INTERVAL}}
    )
    c.indices.refresh(index=TARGET)
    print(f"finalize: refresh_interval restored to {SERVE_REFRESH_INTERVAL}. Run `verify`.")


def _sample_children(c, n: int) -> list[dict[str, Any]]:
    hits = c.search(
        index=SOURCE,
        body={
            "size": n,
            "query": {"term": {"chunk_type": "child"}},
            "_source": ["chunk_id", "embedding"],
        },
    )["hits"]["hits"]
    return [h["_source"] for h in hits if h["_source"].get("embedding")]


def _knn_top_ids(c, index: str, embedding: list[float], k: int = 10) -> list[str]:
    resp = c.search(
        index=index,
        body={
            "size": k,
            "_source": ["chunk_id", "chunk_type"],
            "query": {
                "knn": {
                    "embedding": {
                        "vector": embedding,
                        "k": k,
                        "filter": {"bool": {"filter": [{"term": {"chunk_type": "child"}}]}},
                    }
                }
            },
        },
        request_timeout=60,
    )
    hits = resp["hits"]["hits"]
    non_child = [h for h in hits if h["_source"].get("chunk_type") != "child"]
    if non_child:
        raise AssertionError(f"kNN on {index} returned non-child chunks: {non_child}")
    return [h["_source"]["chunk_id"] for h in hits]


def cmd_verify(c) -> None:
    ok = True

    print("-- count parity --")
    for label, query in [
        ("total", None),
        ("child", {"query": {"term": {"chunk_type": "child"}}}),
        ("parent", {"query": {"term": {"chunk_type": "parent"}}}),
    ]:
        old = c.count(index=SOURCE, body=query)["count"]
        new = c.count(index=TARGET, body=query)["count"]
        match = "OK" if old == new else "MISMATCH"
        if old != new:
            ok = False
        print(f"{label:>7}: old {old:,} | new {new:,}  {match}")

    print("\n-- quantized mapping --")
    emb = c.indices.get_mapping(index=TARGET)[TARGET]["mappings"]["properties"]["embedding"]
    if emb.get("mode") == "on_disk" and emb.get("compression_level") == _expected_level():
        print(f"embedding field: on_disk / {_expected_level()}  OK")
    else:
        ok = False
        print(f"embedding field NOT quantized at {_expected_level()}: {emb}")

    print("\n-- filtered kNN + old/new top-10 overlap --")
    for i, doc in enumerate(_sample_children(c, 3)):
        old_ids = _knn_top_ids(c, SOURCE, doc["embedding"])
        new_ids = _knn_top_ids(c, TARGET, doc["embedding"])
        overlap = len(set(old_ids) & set(new_ids))
        print(f"sample {i + 1} (seed {doc['chunk_id']}): overlap {overlap}/10")
        if overlap < 5:
            ok = False
            print(f"  LOW OVERLAP — old: {old_ids}\n                new: {new_ids}")

    print("\nverify: PASSED — safe to cut config over to mcp-d-quals-v2"
          if ok else "\nverify: FAILED — do NOT cut over; investigate (old index is intact)")
    sys.exit(0 if ok else 1)


def main() -> None:
    global TARGET
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target",
        default=TARGET,
        help=f"target index (default {TARGET}; use mcp-d-quals-16x for the 16x candidate)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    p_create = sub.add_parser("create")
    p_create.add_argument("--recreate", action="store_true",
                          help="drop an existing target index first")
    sub.add_parser("benchmark")
    sub.add_parser("reindex")
    p_status = sub.add_parser("status")
    p_status.add_argument("task_id")
    sub.add_parser("delta")
    sub.add_parser("finalize")
    sub.add_parser("verify")
    args = parser.parse_args()

    TARGET = args.target
    if not TARGET.startswith("mcp-d-quals-"):
        sys.exit(f"refusing target {TARGET!r}: must be an mcp-d-quals-* index (never the source)")

    c = _client()
    if args.cmd == "check":
        cmd_check(c)
    elif args.cmd == "create":
        cmd_create(c, recreate=args.recreate)
    elif args.cmd == "benchmark":
        cmd_benchmark(c)
    elif args.cmd == "reindex":
        cmd_reindex(c)
    elif args.cmd == "status":
        cmd_status(c, args.task_id)
    elif args.cmd == "delta":
        cmd_delta(c)
    elif args.cmd == "finalize":
        cmd_finalize(c)
    elif args.cmd == "verify":
        cmd_verify(c)


if __name__ == "__main__":
    main()
