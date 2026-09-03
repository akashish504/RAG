"""Voyage query-side embedder.

Differences from :mod:`pipeline.embedding_pipeline.embedder.voyage`:

- Uses the ``"query: "`` prefix (not ``"passage: "``).
- Async-friendly: the sync Voyage SDK call runs on a worker thread so the
  router's :func:`asyncio.gather` calls do not block the event loop.
- Request-scoped cache keyed by ``(model, normalised_text)``: identical
  queries within one request reuse the same vector. The cache is bounded
  by ``maxsize`` to prevent unbounded growth in long-running workers.
- Same retry strategy as the indexer's embedder (tenacity, exponential
  backoff + jitter on 429 / 5xx / connection errors).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from typing import Any

import structlog
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = structlog.get_logger(__name__)

_DEFAULT_PREFIX = "query: "
_MAX_ATTEMPTS = 5
_WAIT_MIN_S = 1.0
_WAIT_MAX_S = 60.0


def _is_retryable(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if any(s in msg for s in ("429", "rate limit", "503", "502", "500", "timeout", "connection")):
        return True
    http_status: int | None = getattr(exc, "http_status", None) or getattr(exc, "status_code", None)
    return http_status in (429, 500, 502, 503, 504) if http_status is not None else False


class VoyageQueryEmbedder:
    """Voyage-4 (or compatible) query embedder."""

    model: str
    dims: int

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "voyage-4",
        dims: int = 1024,
        query_prefix: str = _DEFAULT_PREFIX,
        cache_size: int = 256,
        max_retries: int = _MAX_ATTEMPTS,
    ) -> None:
        try:
            import voyageai  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "Voyage query embedding requires voyageai. "
                "Install with: pip install 'dalberg-mcp[voyage]'"
            ) from exc

        self.model = model
        self.dims = dims
        self._prefix = query_prefix
        self._max_retries = max_retries
        self._cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self._cache_size = cache_size
        self._cache_lock = threading.Lock()
        self._client: Any = voyageai.Client(api_key=api_key)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def embed_query(self, text: str) -> list[float]:
        if not text:
            msg = "Cannot embed empty query"
            raise ValueError(msg)
        norm = text.strip()
        if not norm:
            raise ValueError("Cannot embed whitespace-only query")
        cache_key = (self.model, norm)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        embedding = await asyncio.to_thread(self._embed_sync, norm)
        if len(embedding) != self.dims:
            msg = (
                f"Voyage returned {len(embedding)}-dim vector; expected {self.dims}. "
                "Check VOYAGE_EMBED_DIMS / model compatibility."
            )
            log.error("voyage_query_dim_mismatch", got=len(embedding), expected=self.dims)
            raise ValueError(msg)
        self._cache_put(cache_key, embedding)
        return embedding

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cache_get(self, key: tuple[str, str]) -> list[float] | None:
        with self._cache_lock:
            value = self._cache.get(key)
            if value is not None:
                self._cache.move_to_end(key)
            return value

    def _cache_put(self, key: tuple[str, str], value: list[float]) -> None:
        with self._cache_lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    def _embed_sync(self, text: str) -> list[float]:
        @retry(
            wait=wait_exponential_jitter(initial=_WAIT_MIN_S, max=_WAIT_MAX_S),
            stop=stop_after_attempt(self._max_retries),
            retry=retry_if_exception(_is_retryable),
            before_sleep=before_sleep_log(logging.getLogger(__name__), logging.WARNING),
            reraise=True,
        )
        def _call() -> list[float]:
            result = self._client.embed([f"{self._prefix}{text}"], model=self.model)
            return result.embeddings[0]

        return _call()


_EMBEDDER: VoyageQueryEmbedder | None = None


def get_query_embedder() -> VoyageQueryEmbedder:
    """Lazy module-level embedder built from runtime + retrieval config."""

    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER

    from retrieval.config import load_config  # noqa: PLC0415
    from retrieval.settings import get_runtime_settings  # noqa: PLC0415

    runtime = get_runtime_settings()
    if not runtime.voyage_api_key:
        raise RuntimeError(
            "VOYAGE_API_KEY is not set; cannot embed queries. "
            "Either set the env var or restrict requests to mode='airtable_only'."
        )
    cfg = load_config().embedding
    _EMBEDDER = VoyageQueryEmbedder(
        api_key=runtime.voyage_api_key,
        model=cfg.model,
        dims=cfg.dims,
        query_prefix=cfg.query_prefix,
    )
    return _EMBEDDER
