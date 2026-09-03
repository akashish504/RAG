"""Voyage query-side reranker.

Uses a Voyage reranker model (e.g. ``rerank-2.5``) to
re-score a candidate list *after* the initial KNN+BM25+RRF retrieval and
*before* person-level deduplication in :mod:`retrieval.sources.opensearch`.

Design mirrors :mod:`retrieval.embedding.voyage`:
- Sync Voyage SDK call offloaded to a worker thread so the async router is
  never blocked.
- Tenacity retry on 429 / 5xx / connection errors with exponential backoff.
- Module-level lazy singleton via :func:`get_reranker` so the client is
  built once and reused across requests.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import structlog
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

if TYPE_CHECKING:
    from retrieval.models import SearchResult

log = structlog.get_logger(__name__)

_MAX_ATTEMPTS = 5
_WAIT_MIN_S = 1.0
_WAIT_MAX_S = 60.0
# Voyage reranker has an input character limit per document; truncate to be safe.
# Dense D.Quals slides (full table + visual descriptions) routinely exceed 2k chars,
# so a low cap made the reranker score a truncated slide. 5k keeps the whole slide.
_MAX_DOC_CHARS = 5_000


def _is_retryable(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if any(s in msg for s in ("429", "rate limit", "503", "502", "500", "timeout", "connection")):
        return True
    http_status: int | None = getattr(exc, "http_status", None) or getattr(exc, "status_code", None)
    return http_status in (429, 500, 502, 503, 504) if http_status is not None else False


class VoyageReranker:
    """Voyage cross-encoder reranker (rerank-2, rerank-2.5, etc.)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "rerank-2.5",
        max_retries: int = _MAX_ATTEMPTS,
    ) -> None:
        try:
            import voyageai  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "Voyage reranking requires voyageai. "
                "Install with: pip install 'dalberg-mcp[voyage]'"
            ) from exc

        self.model = model
        self._max_retries = max_retries
        self._client: Any = voyageai.Client(api_key=api_key)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def rerank(
        self,
        query: str,
        hits: list["SearchResult"],
    ) -> list["SearchResult"]:
        """Re-score *hits* against *query* and return sorted by relevance.

        The original ``hits`` list is mutated in-place (scores updated) so
        callers that hold references to individual results also see the new
        scores. The returned list is a *new* sorted list — original order
        is preserved on the hits themselves via the updated ``score`` field.

        Parameters
        ----------
        query:
            The search query string (plain text, no prefix).
        hits:
            Candidate results from the KNN+BM25+RRF stage.  Each hit's
            ``text`` field is sent to the reranker; longer texts are
            truncated to :data:`_MAX_DOC_CHARS` characters.

        Returns
        -------
        list[SearchResult]
            The same hit objects sorted by descending relevance score.
            Returns *hits* unchanged (original order) if the API call fails.
        """
        if not hits or not query.strip():
            return hits

        texts = [(h.text or "")[:_MAX_DOC_CHARS] for h in hits]

        try:
            result = await asyncio.to_thread(
                self._rerank_sync,
                query=query,
                documents=texts,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("voyage_reranker_failed", model=self.model, error=str(exc))
            return hits

        # ``result.results`` is already sorted by descending relevance_score.
        for r in result.results:
            hits[r.index].score = float(r.relevance_score)

        return [hits[r.index] for r in result.results]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rerank_sync(self, *, query: str, documents: list[str]) -> Any:
        @retry(
            wait=wait_exponential_jitter(initial=_WAIT_MIN_S, max=_WAIT_MAX_S),
            stop=stop_after_attempt(self._max_retries),
            retry=retry_if_exception(_is_retryable),
            before_sleep=before_sleep_log(logging.getLogger(__name__), logging.WARNING),
            reraise=True,
        )
        def _call() -> Any:
            return self._client.rerank(
                query=query,
                documents=documents,
                model=self.model,
                top_k=len(documents),
            )

        return _call()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_RERANKER: VoyageReranker | None = None


def get_reranker() -> VoyageReranker:
    """Lazy module-level reranker built from runtime + retrieval config."""

    global _RERANKER
    if _RERANKER is not None:
        return _RERANKER

    from retrieval.config import load_config  # noqa: PLC0415
    from retrieval.settings import get_runtime_settings  # noqa: PLC0415

    runtime = get_runtime_settings()
    if not runtime.voyage_api_key:
        raise RuntimeError(
            "VOYAGE_API_KEY is not set; cannot rerank results. "
            "Set reranker_enabled: false in ranking config or provide the key."
        )
    cfg = load_config().ranking
    _RERANKER = VoyageReranker(
        api_key=runtime.voyage_api_key,
        model=cfg.reranker_model,
    )
    return _RERANKER
