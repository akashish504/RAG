"""Per-deck mutable state shared across vLLM worker threads."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from pipeline.preprocessing.slides.backends import SlideContent
from pipeline.preprocessing.slides.batch import DeckJob


@dataclass
class DeckState:
    """Accumulates per-slide VLM results across worker threads for one deck.

    The render worker sets ``base_slides`` and ``remaining`` before pushing
    any slide items to the queue. Each vLLM worker thread:
      1. stores its SlideContent in ``results``
      2. decrements ``remaining`` under ``lock``
      3. if ``remaining`` hits zero, that thread owns finalization

    ``done`` is set to True only after a successful S3 write.
    """
    job: DeckJob
    # slide_number -> SlideContent, filled in by vLLM worker threads
    results: dict[int, SlideContent] = field(default_factory=dict)
    # counts down as visual slides return; the thread that hits 0 finalizes
    remaining: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    # parser-only base slides set by render worker (slide_number -> SlideExtraction)
    base_slides: dict = field(default_factory=dict)
    done: bool = False
    error: str | None = None
