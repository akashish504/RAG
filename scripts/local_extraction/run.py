"""Entry point for parallel local-VLM slide extraction.

Usage (on GPU EC2 with vLLM already serving):

    vllm serve OpenGVLab/InternVL2_5-8B \\
        --host 0.0.0.0 --port 8000 \\
        --max-num-seqs 64 \\
        --dtype fp8

    python -m scripts.local_extraction.run \\
        --prefix raw/d.quals/ \\
        --model OpenGVLab/InternVL2_5-8B \\
        --vllm-url http://localhost:8000 \\
        --render-workers 20 \\
        --vllm-concurrency 48

Tuning guide:
    --render-workers   One LibreOffice process per worker. On 32 vCPUs, 20 is a
                       safe default (leaves headroom for vLLM + OS). Raise to 28
                       if GPU is idle while render workers are busy.
    --vllm-concurrency Concurrent HTTP threads hitting vLLM. vLLM's continuous
                       batching packs them onto the GPU. Match to vLLM's
                       --max-num-seqs; 48 for --max-num-seqs 64 is a good start.
    --queue-depth      Slides buffered between render and vLLM pools. 200 is
                       enough for ~10 decks in flight simultaneously.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from pipeline.common.aws import s3_client  # noqa: E402
from pipeline.preprocessing.slides.render import PptxSlideRenderer  # noqa: E402

from .deck_state import DeckState  # noqa: E402
from .discovery import discover_jobs  # noqa: E402
from .render_worker import STOP, render_worker  # noqa: E402
from .s3_store import S3Store  # noqa: E402
from .vlm_client import VLMClient  # noqa: E402
from .vlm_worker import vlm_worker  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parallel local-VLM PPTX extraction → S3 normalized.txt"
    )
    parser.add_argument("--prefix", default="raw/d.quals/",
                        help="S3 prefix to scan (default: raw/d.quals/)")
    parser.add_argument("--model", required=True,
                        help="vLLM model name, e.g. OpenGVLab/InternVL2_5-8B")
    parser.add_argument("--vllm-url", default="http://localhost:8000",
                        help="vLLM base URL (default: http://localhost:8000)")
    parser.add_argument("--render-workers", type=int, default=20,
                        help="Parallel LibreOffice render threads (default: 20)")
    parser.add_argument("--vllm-concurrency", type=int, default=48,
                        help="Concurrent HTTP threads to vLLM (default: 48)")
    parser.add_argument("--queue-depth", type=int, default=200,
                        help="Slide buffer between render and vLLM pools (default: 200)")
    parser.add_argument("--max-tokens", type=int, default=2048,
                        help="Max tokens per vLLM call (default: 2048)")
    parser.add_argument("--dpi", type=int, default=110,
                        help="Render DPI — lower saves VRAM, 110 keeps labels legible (default: 110)")
    parser.add_argument("--timeout", type=int, default=300,
                        help="vLLM HTTP timeout in seconds (default: 300)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"),
                        help="AWS region (default: eu-west-1)")
    parser.add_argument("--bucket", default=os.environ.get("S3_BUCKET"),
                        help="S3 bucket (or set S3_BUCKET env var)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N decks — useful for smoke-testing")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )

    bucket = (args.bucket or "").strip()
    if not bucket:
        raise SystemExit("Missing S3_BUCKET — set env var or pass --bucket")

    store = S3Store(client=s3_client(region_name=args.region), bucket=bucket)
    renderer = PptxSlideRenderer(dpi=args.dpi)
    vlm = VLMClient(
        model=args.model,
        base_url=args.vllm_url,
        max_tokens=args.max_tokens,
        timeout_s=args.timeout,
    )

    jobs = discover_jobs(store, args.prefix)
    if not jobs:
        logging.getLogger(__name__).info("Nothing to do — all decks already extracted.")
        return
    if args.limit:
        jobs = jobs[: args.limit]
        logging.getLogger(__name__).info(
            "--limit %d applied: processing first %d deck(s)", args.limit, len(jobs)
        )

    # Per-deck state shared across threads
    states: dict[str, DeckState] = {j.custom_id: DeckState(job=j) for j in jobs}
    progress_file = PROJECT_ROOT / "completed_records.txt"
    stats: dict = {"written": 0, "progress_file": str(progress_file)}
    stats_lock = threading.Lock()

    # Bounded queue: back-pressures render workers when vLLM is saturated
    slide_queue: queue.Queue = queue.Queue(maxsize=args.queue_depth)

    # Distribute jobs across render workers (round-robin)
    n_render = min(args.render_workers, len(jobs))
    shards: list[list] = [[] for _ in range(n_render)]
    for i, job in enumerate(jobs):
        shards[i % n_render].append(job)

    log = logging.getLogger(__name__)
    t0 = time.time()
    log.info(
        "Starting: %d deck(s) | %d render workers | %d vLLM threads | queue depth %d",
        len(jobs), n_render, args.vllm_concurrency, args.queue_depth,
    )

    # Start vLLM consumer threads first — they block on the empty queue
    vlm_threads = [
        threading.Thread(
            target=vlm_worker,
            args=(slide_queue, states, store, vlm, stats, stats_lock),
            daemon=True,
            name=f"vlm-{i}",
        )
        for i in range(args.vllm_concurrency)
    ]
    for t in vlm_threads:
        t.start()

    # Start render threads — they immediately begin pushing to the queue
    render_threads = [
        threading.Thread(
            target=render_worker,
            args=(shard, store, renderer, slide_queue, states),
            daemon=True,
            name=f"render-{i}",
        )
        for i, shard in enumerate(shards)
    ]
    for t in render_threads:
        t.start()

    # Wait for all render workers to finish pushing work
    for t in render_threads:
        t.join()
    log.info("All render workers done; draining vLLM queue ...")

    # Signal each vLLM worker to stop after finishing its current item
    for _ in vlm_threads:
        slide_queue.put(STOP)
    for t in vlm_threads:
        t.join()

    elapsed = time.time() - t0
    n_written = stats["written"]
    n_error = sum(1 for s in states.values() if s.error)
    log.info(
        "Extraction complete: %d written, %d errors, %.0fs elapsed (%.1f decks/min)",
        n_written, n_error, elapsed, n_written / max(1, elapsed) * 60,
    )

    log.info("All done.")


if __name__ == "__main__":
    main()
