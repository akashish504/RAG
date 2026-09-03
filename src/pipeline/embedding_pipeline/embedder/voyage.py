"""Voyage-4 embedder with batching, retries, and rate-limit handling.

Requires the ``voyage`` extra:
    pip install 'dalberg-mcp[voyage]'

Design
------
- Only **child** chunks are embedded (parents are skipped).
- The ``passage: `` prefix is prepended to each text here, keeping prefix
  policy inside the embedder rather than leaking into the chunker.
- Batches of ``batch_size`` texts (default 32) are sent in sequence.
  Voyage's API limit is 128 inputs per request; we use 32 to stay well
  within rate limits and keep individual request latency low.
- ``tenacity`` retries on rate-limit (429) and transient server errors
  (5xx / connection errors) with exponential backoff + jitter. On
  non-retryable errors the batch is skipped and the error is appended to
  ``EmbedReport.errors`` so the pipeline run continues.
- ``embedding_model`` is written into ``chunk.metadata`` on every embedded
  chunk so OpenSearch records which model version produced the vector.

SQS compatibility
-----------------
The embedder is fully synchronous. When an SQS worker is introduced, it
will call ``Pipeline.run_one(s3_key)`` synchronously from its handler
function — no async changes required here.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import structlog
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from pipeline.embedding_pipeline.models import Chunk, ChunkType, EmbedReport

log = structlog.get_logger(__name__)

# Prefix required by Voyage for index-time passage embeddings.
_PASSAGE_PREFIX = "passage: "

# Retry budget: up to 5 attempts, 1s → 60s back-off + jitter.
_MAX_ATTEMPTS = 5
_WAIT_MIN_S = 1.0
_WAIT_MAX_S = 60.0


def _is_retryable(exc: BaseException) -> bool:
    """Return True for 429 / 5xx / connection errors that warrant a retry."""
    msg = str(exc).lower()
    retryable_signals = ("429", "rate limit", "503", "502", "500", "timeout", "connection")
    if any(signal in msg for signal in retryable_signals):
        return True
    # voyageai SDK exposes http_status on its error classes.
    http_status: int | None = getattr(exc, "http_status", None) or getattr(
        exc, "status_code", None
    )
    if http_status is not None:
        return http_status in (429, 500, 502, 503, 504)
    return False


class VoyageEmbedder:
    """Embed child chunks via the Voyage-4 API.

    Parameters
    ----------
    api_key:
        Voyage API key (``VOYAGE_API_KEY`` env var in production).
    model:
        Voyage model name.  Override here to switch to a future model
        version without any other code changes (embedding versioning).
    dims:
        Expected embedding dimensionality.  Must match ``INDEX_MAPPING``.
    batch_size:
        Number of texts per API call.  Voyage supports up to 128;
        32 is the recommended production default.
    max_retries:
        Maximum tenacity retry attempts per batch.
    """

    model: str
    dims: int

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "voyage-4",
        dims: int = 1024,
        batch_size: int = 32,
        max_retries: int = _MAX_ATTEMPTS,
    ) -> None:
        try:
            import voyageai  # noqa: PLC0415
        except ImportError as exc:
            msg = (
                "Voyage embedding requires voyageai. "
                "Install with: pip install 'dalberg-mcp[voyage]'"
            )
            raise ImportError(msg) from exc

        self.model = model
        self.dims = dims
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._client: Any = voyageai.Client(api_key=api_key)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def embed(self, chunks: list[Chunk]) -> EmbedReport:
        """Embed child chunks in place.  Skips non-child chunks silently."""
        report = EmbedReport()
        child_chunks = [c for c in chunks if c.chunk_type is ChunkType.CHILD]

        if not child_chunks:
            return report

        total_batches = math.ceil(len(child_chunks) / self._batch_size)
        log.info(
            "voyage_embed_start",
            child_chunks=len(child_chunks),
            batches=total_batches,
            model=self.model,
        )

        for batch_idx in range(total_batches):
            start = batch_idx * self._batch_size
            batch = child_chunks[start : start + self._batch_size]
            texts = [f"{_PASSAGE_PREFIX}{c.embed_text or c.text}" for c in batch]

            try:
                embeddings, tokens = self._embed_batch_with_retry(texts)
                report.batches_sent += 1
                report.total_tokens += tokens

                for chunk, vector in zip(batch, embeddings):
                    if len(vector) != self.dims:
                        msg = (
                            f"Voyage returned {len(vector)}-dim vector; "
                            f"expected {self.dims}. chunk_id={chunk.chunk_id}"
                        )
                        log.error("dimension_mismatch", detail=msg)
                        report.errors.append(msg)
                        continue
                    chunk.embedding = vector
                    chunk.metadata["embedding_model"] = self.model
                    report.chunks_embedded += 1

            except Exception as exc:  # noqa: BLE001
                error_msg = (
                    f"batch {batch_idx + 1}/{total_batches} failed "
                    f"after {self._max_retries} attempts: {exc}"
                )
                log.error("voyage_batch_failed", batch=batch_idx + 1, error=str(exc))
                report.errors.append(error_msg)

        log.info(
            "voyage_embed_complete",
            chunks_embedded=report.chunks_embedded,
            batches_sent=report.batches_sent,
            total_tokens=report.total_tokens,
            errors=len(report.errors),
        )
        return report

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _embed_batch_with_retry(self, texts: list[str]) -> tuple[list[list[float]], int]:
        """Call Voyage API with tenacity retries.

        Returns (embeddings, total_tokens).
        Raises the final exception if all retries are exhausted.
        """
        # Build a retry-decorated closure bound to self so _max_retries is
        # respected per instance rather than hard-coded at class level.
        @retry(
            wait=wait_exponential_jitter(initial=_WAIT_MIN_S, max=_WAIT_MAX_S),
            stop=stop_after_attempt(self._max_retries),
            retry=retry_if_exception(_is_retryable),
            before_sleep=before_sleep_log(
                logging.getLogger(__name__), logging.WARNING
            ),
            reraise=True,
        )
        def _call() -> tuple[list[list[float]], int]:
            result = self._client.embed(texts, model=self.model)
            tokens: int = getattr(result, "total_tokens", 0) or 0
            return result.embeddings, tokens

        return _call()
