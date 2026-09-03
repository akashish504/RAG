"""Source adapter protocols.

A *logical source* (e.g. ``dalberg_profiles``) bundles two adapter views:

- :class:`AirtableSourceProtocol` — structured rows accessible via Airtable
  ``filterByFormula`` queries.
- :class:`OpenSearchSourceProtocol` — embedded chunks accessible via KNN +
  BM25 hybrid search.

Both protocols return :class:`retrieval.models.SearchResult` objects so the
router can merge hits from heterogeneous backends without special-casing.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from retrieval.models import (
    Hint,
    RetrievalQuery,
    SchemaDescriptor,
    SearchResult,
)


@runtime_checkable
class AirtableSourceProtocol(Protocol):
    """Structured Airtable side of a logical source."""

    name: str
    display_name: str

    async def filter_structured(
        self,
        *,
        formula: str | None,
        fields: list[str] | None,
        max_records: int | None,
    ) -> tuple[list[SearchResult], list[Hint]]:
        """Run an Airtable ``listRecords`` call with optional filterByFormula.

        Returns the rows plus any cross-tool hints (e.g. flagged long-text
        truncations).
        """
        ...

    def get_schema(self) -> SchemaDescriptor:
        """Return the merged source schema (Airtable + OpenSearch metadata)."""
        ...

    def supported_filters(self) -> set[str]:
        ...


@runtime_checkable
class OpenSearchSourceProtocol(Protocol):
    """Semantic OpenSearch side of a logical source."""

    name: str
    display_name: str
    index_name: str

    async def search_semantic(
        self,
        *,
        query: str,
        embedding: list[float] | None,
        top_k: int,
        filters: dict[str, Any],
    ) -> tuple[list[SearchResult], list[Hint]]:
        """Run KNN + BM25 hybrid (or KNN-only / BM25-only) and return hits.

        Hits are *parent* chunks hydrated via ``mget`` on ``parent_chunk_id``
        from the underlying child KNN matches. Score is normalised to
        ``[0, 1]`` before return.
        """
        ...

    async def fetch_by_id(self, ids: list[str]) -> list[SearchResult]:
        """Hydrate specific chunks (parent or child) by ``chunk_id``."""
        ...

    def supported_filters(self) -> set[str]:
        ...


@runtime_checkable
class RetrievalSource(Protocol):
    """A logical source that may expose structured + semantic views.

    Concrete implementation: :class:`retrieval.config.LogicalSource`.
    """

    name: str
    display_name: str
    description: str | None
    enabled: bool

    @property
    def airtable(self) -> AirtableSourceProtocol | None: ...

    @property
    def opensearch(self) -> OpenSearchSourceProtocol | None: ...

    def capabilities(self) -> list[str]:
        """Returns subset of ``["semantic", "structured"]`` supported."""
        ...

    def get_schema(self) -> SchemaDescriptor: ...

    async def query(self, q: RetrievalQuery) -> tuple[list[SearchResult], list[Hint]]:
        """Dispatch to the appropriate adapter(s) according to ``q.mode``."""
        ...
