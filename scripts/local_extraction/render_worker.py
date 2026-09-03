"""Render worker: S3 get → LibreOffice → PNG → push slide items to queue.

One thread per shard of jobs. LibreOffice runs as a subprocess so it releases
the GIL — N render threads genuinely run in parallel on N cores.
"""

from __future__ import annotations

import logging
import queue
from pathlib import PurePosixPath
from typing import Any

from pipeline.preprocessing.slides.enricher import _base_slide
from pipeline.preprocessing.slides.extractor import attaches_image
from pipeline.preprocessing.slides.render import PptxSlideRenderer

from .deck_state import DeckState
from .parser import PAGE_NUM_RE
from .s3_store import S3Store

log = logging.getLogger(__name__)

# Sentinel pushed to the slide queue to signal a vLLM worker to stop
STOP = object()


def render_worker(
    jobs: list,                      # list[DeckJob] — this thread's shard
    store: S3Store,
    renderer: PptxSlideRenderer,
    slide_queue: "queue.Queue[Any]",
    states: dict[str, DeckState],
) -> None:
    """Process each assigned deck serially: fetch → render → enqueue slides."""
    for job in jobs:
        # Fetch meta here (deferred from discovery to avoid thousands of serial
        # S3 GETs during the initial LIST scan).
        attach_dir = PurePosixPath(job.original_key).parent
        meta = store.get_json(f"{attach_dir.as_posix()}/.airtable_meta.json")
        if meta:
            job.identifier = str(meta.get("identifier") or job.identifier)
            job.record_id = str(meta.get("airtable_record_id") or "")
            job.column = str(meta.get("column_name") or "")
            job.metadata_header = str(meta.get("metadata_header") or "")
            job.base_id = str(meta.get("airtable_base_id") or "")
            job.table_id = str(meta.get("airtable_table_id") or "")
            job.facets = meta.get("facets") or {}

        log.info("[render] record=%s att=%s — fetching from S3 ...",
                 job.identifier, job.custom_id)
        try:
            pptx_bytes = store.get_bytes(job.original_key)
        except Exception:
            log.warning("[render] record=%s att=%s — S3 get failed, skipping",
                        job.identifier, job.custom_id, exc_info=True)
            states[job.custom_id].error = "s3_get_failed"
            continue

        try:
            rendered = renderer.render(pptx_bytes)
        except Exception:
            log.warning("[render] record=%s att=%s — render failed, skipping",
                        job.identifier, job.custom_id, exc_info=True)
            states[job.custom_id].error = "render_failed"
            continue

        state = states[job.custom_id]

        # Build parser base for every slide; strip standalone page-number lines
        base = {r.slide_number: _base_slide(r) for r in rendered}
        for s in base.values():
            s.text = PAGE_NUM_RE.sub("", s.text).strip()

        visual_slides = [r for r in rendered if attaches_image(r) and r.image_png]

        with state.lock:
            state.base_slides = base
            state.remaining = len(visual_slides)

        log.info("[render] record=%s att=%s — %d slides total, %d visual → queue",
                 job.identifier, job.custom_id, len(rendered), len(visual_slides))

        if not visual_slides:
            # Text-only deck: no vLLM slide calls needed; signal direct finalization
            slide_queue.put(("text_only", job.custom_id))
        else:
            for r in visual_slides:
                slide_queue.put(("slide", job.custom_id, r.slide_number, r.image_png))
