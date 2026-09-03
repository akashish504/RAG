"""vLLM worker: drain slide queue → inference → finalize deck when last slide returns.

Each thread loops on the queue independently. Threads share no mutable state
except the per-deck DeckState objects (protected by per-deck locks).

Finalization (merge + summarize + S3 write) is done by whichever thread
decrements a deck's remaining count to zero — no dedicated finalizer thread
needed, and no deck blocks any other deck.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from pathlib import PurePosixPath
from typing import Any

from pipeline.preprocessing.slides.backends import SlideContent
from pipeline.preprocessing.slides.enricher import _merge_enrichment
from pipeline.preprocessing.slides.models import DeckExtraction

from .deck_state import DeckState
from .record_summary import update_record_summary
from .render_worker import STOP
from .s3_store import S3Store
from .vlm_client import VLMClient, DECK_DIGEST_MAX_CHARS

log = logging.getLogger(__name__)


def vlm_worker(
    slide_queue: "queue.Queue[Any]",
    states: dict[str, DeckState],
    store: S3Store,
    vlm: VLMClient,
    stats: dict,
    stats_lock: threading.Lock,
) -> None:
    """Pull one item at a time from the queue; return to queue after each call."""
    while True:
        item = slide_queue.get()
        if item is STOP:
            slide_queue.task_done()
            break

        kind = item[0]

        if kind == "text_only":
            _, deck_id = item
            _finalize_deck(states[deck_id], store, vlm, stats, stats_lock)
            slide_queue.task_done()
            continue

        # kind == "slide": one image → one vLLM call
        _, deck_id, slide_number, png = item
        state = states[deck_id]

        try:
            content = vlm.extract_slide(png)
            log.info("[vlm] record=%s att=%s slide=%d — title=%r text_len=%d",
                     state.job.identifier, deck_id, slide_number,
                     content.title, len(content.text))
        except Exception:
            log.warning("[vlm] record=%s att=%s slide=%d — call failed, using parser output",
                        state.job.identifier, deck_id, slide_number, exc_info=True)
            content = SlideContent(title=None, text="", tables=[], visuals="")

        with state.lock:
            state.results[slide_number] = content
            state.remaining -= 1
            finalize = state.remaining == 0

        # Only the thread that hit zero finalizes — no double-write possible
        if finalize:
            _finalize_deck(state, store, vlm, stats, stats_lock)

        slide_queue.task_done()


def _finalize_deck(
    state: DeckState,
    store: S3Store,
    vlm: VLMClient,
    stats: dict,
    stats_lock: threading.Lock,
) -> None:
    """Merge VLM results into parser base, summarize, write normalized.txt."""
    job = state.job
    base = state.base_slides

    if not base:
        log.warning("[finalize] %s — no base slides, skipping", job.custom_id)
        return

    # Merge each visual slide's VLM output into its parser base
    for slide_number, content in state.results.items():
        target = base.get(slide_number)
        if target is not None:
            _merge_enrichment(target, content, extractor_name="local_vlm")

    slides = [base[n] for n in sorted(base)]

    # Post-pass title cleanup on ALL slides (VLM-enriched and parser-only):
    # (a) untitled slides: promote first text line as title
    # (b) titles with a leading "- " bullet from python-pptx shapes: strip the dash
    for s in slides:
        if not (s.title or "").strip() and s.text.strip():
            first_line = s.text.split("\n")[0].strip()
            clean = re.sub(r"^-\s+", "", first_line).strip()
            if clean:
                s.title = clean
                s.text = s.text[len(first_line):].strip()
        elif s.title:
            s.title = re.sub(r"^-\s+", "", s.title.strip())

    # Build capped digest for the summary call (respects InternVL 8K context)
    per_slide_chars = max(100, DECK_DIGEST_MAX_CHARS // max(1, len(slides)))
    parts: list[str] = []
    for s in slides:
        head = f"Slide {s.slide_number}: {s.title or ''}".strip()
        body = "\n".join(
            p for p in (s.text, *s.tables, s.visuals) if p.strip()
        )[:per_slide_chars]
        parts.append(f"{head}\n{body}".strip())
    digest = "\n\n".join(parts)[:DECK_DIGEST_MAX_CHARS]

    summary: str | None = None
    if digest.strip():
        try:
            summary = vlm.summarize_deck(digest) or None
        except Exception:
            log.warning("[finalize] %s — summarize_deck failed", job.custom_id, exc_info=True)

    deck = DeckExtraction(
        document_id=PurePosixPath(job.original_key).stem or "deck",
        source_s3_key=job.original_key,
        summary=summary,
        slides=slides,
    )

    try:
        markdown = job.metadata_header + deck.to_markdown()
        # Collapse floods of blank lines (VLM sometimes emits many \n in a row)
        markdown = re.sub(r"\n{3,}", "\n\n", markdown)
        store.put_text(job.normalized_key, markdown)
        log.info("[finalize] record=%s att=%s — written to S3", job.identifier, job.custom_id)
    except Exception:
        log.warning("[finalize] record=%s att=%s — S3 write failed",
                    job.identifier, job.custom_id, exc_info=True)
        state.error = "s3_write_failed"
        return

    state.done = True

    # Update record summary immediately — reads all S3 normalized.txt for this
    # record, merges with the new deck, rewrites __record_summary.txt in place.
    try:
        update_record_summary(job, store, vlm)
    except Exception:
        log.warning("[finalize] record=%s att=%s — record summary update failed",
                    job.identifier, job.custom_id, exc_info=True)
    with stats_lock:
        stats["written"] += 1
        # Append completed record identifier to progress file (thread-safe via lock)
        try:
            with open(stats["progress_file"], "a") as f:
                f.write(f"{job.identifier}\n")
        except Exception:
            log.warning("[finalize] could not write progress file", exc_info=True)
