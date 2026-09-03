"""Reader protocol."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from pipeline.embedding_pipeline.models import Document


class Reader(Protocol):
    """Any input adapter that yields source Documents."""

    def iter_documents(self, prefix: str | None = None) -> Iterator[Document]:
        """Yield documents under a prefix."""

    def get_document(self, key: str) -> Document:
        """Fetch one document by storage key."""
