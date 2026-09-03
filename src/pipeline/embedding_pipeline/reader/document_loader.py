"""DocumentLoader — orchestrates table-aware reading and parsing.

Responsibilities:
  - Look up the right ``TableConfig`` by S3 key prefix.
  - Use the configured ``S3Reader`` to fetch raw bytes.
  - Filter out objects whose extension is not in
    ``TableConfig.supported_extensions`` (defaults to ``.txt`` only;
    non-text objects are produced by the upstream ingestion pipeline).
  - Use ``ParserRegistry`` to turn bytes into a ``ParsedDocument``.
  - Yield ``LoadedDocument`` objects ready for the chunker.

The chunker itself is intentionally not part of this loader so the read /
parse phase can be exercised on its own.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

import structlog

from pipeline.embedding_pipeline.models import Document
from pipeline.embedding_pipeline.parser.base import ParsedDocument
from pipeline.embedding_pipeline.parser.registry import ParserRegistry
from pipeline.embedding_pipeline.reader.base import Reader
from pipeline.embedding_pipeline.reader.tables import TableConfig, TableRegistry

log = structlog.get_logger(__name__)

# Raw binary originals are inputs to NORMALIZATION, never embedded directly. If
# one is skipped (because the table embeds .txt only), it means extraction hasn't
# produced its ``__normalized.txt`` yet — warn loudly rather than silently drop it.
_RAW_BINARY_EXTS = frozenset({".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xlsm"})


@dataclass(slots=True)
class LoadedDocument:
    """A read + parsed document, ready for chunking."""

    document: Document
    parsed: ParsedDocument
    table: TableConfig


class DocumentLoader:
    """Bind the reader, parser registry, and table registry together."""

    def __init__(
        self,
        *,
        reader: Reader,
        parser_registry: ParserRegistry,
        table_registry: TableRegistry,
    ) -> None:
        self.reader = reader
        self.parser_registry = parser_registry
        self.table_registry = table_registry

    def iter_table(self, table_name: str) -> Iterator[LoadedDocument]:
        """Iterate every supported document belonging to ``table_name``."""

        table = self.table_registry.get(table_name)
        for document in self.reader.iter_documents(table.s3_prefix):
            if not self._is_supported(table, document.source.s3_key):
                self._warn_unnormalized_binary(table, document.source.s3_key)
                continue
            yield self._load(document, table)

    def iter_all(self) -> Iterator[LoadedDocument]:
        """Iterate every supported document across every registered table."""

        for table in self.table_registry.list():
            yield from self.iter_table(table.name)

    def iter_prefix(self, prefix: str) -> Iterator[LoadedDocument]:
        """Iterate every supported document under an arbitrary S3 prefix.

        Unlike ``iter_table``, this method is not constrained to a single
        table's configured ``s3_prefix``.  Table resolution is done on each
        key via ``TableRegistry.resolve_from_key``; keys that do not match
        any registered table are skipped.

        This is the method used by ``Pipeline.run(prefix)`` and is the natural
        hook for a future SQS worker that receives an S3 key directly.
        """
        for document in self.reader.iter_documents(prefix):
            table = self.table_registry.resolve_from_key(document.source.s3_key)
            if table is None:
                continue
            if not self._is_supported(table, document.source.s3_key):
                self._warn_unnormalized_binary(table, document.source.s3_key)
                continue
            yield self._load(document, table)

    def load_one(self, s3_key: str) -> LoadedDocument:
        """Load and parse a single object by exact S3 key."""

        table = self.table_registry.resolve_from_key(s3_key)
        if table is None:
            msg = (
                f"S3 key {s3_key!r} does not match any registered table prefix. "
                f"Known prefixes: {[t.s3_prefix for t in self.table_registry.list()]}"
            )
            raise ValueError(msg)
        if not self._is_supported(table, s3_key):
            msg = (
                f"S3 key {s3_key!r} has an unsupported extension for table "
                f"{table.name!r}. Allowed: {list(table.supported_extensions)}"
            )
            raise ValueError(msg)
        document = self.reader.get_document(s3_key)
        return self._load(document, table)

    @staticmethod
    def _is_supported(table: TableConfig, s3_key: str) -> bool:
        if not table.supported_extensions:
            return True
        ext = PurePosixPath(s3_key).suffix.lower()
        return ext in table.supported_extensions

    @staticmethod
    def _warn_unnormalized_binary(table: TableConfig, s3_key: str) -> None:
        """Loud signal: a raw binary was skipped (never embedded directly). It has
        no ``__normalized.txt`` yet — run extraction. Prevents silent raw-binary
        indexing from ever recurring."""
        if PurePosixPath(s3_key).suffix.lower() in _RAW_BINARY_EXTS:
            log.warning(
                "raw_binary_skipped_not_embedded",
                key=s3_key,
                table=table.name,
                hint="no __normalized.txt yet — run extraction; raw binaries are never embedded",
            )

    def _load(self, document: Document, table: TableConfig) -> LoadedDocument:
        # The raw S3 path segment can differ from the registry table name — e.g. the
        # DOTTED prefix `raw/d.quals/` parses to table_name "d.quals" while the table
        # is registered as "d_quals". Stamp the RESOLVED registry name so the indexer
        # routes chunks to the right index (was silently falling back to mcp-docs) and
        # the indexed `table_name` field is consistent.
        if document.source.table_name != table.name:
            document.source = replace(document.source, table_name=table.name)
        parser = self.parser_registry.get_for_key(document.source.s3_key)
        body = document.body if document.body is not None else document.text.encode("utf-8")
        parsed = parser.parse(body, key=document.source.s3_key)
        return LoadedDocument(document=document, parsed=parsed, table=table)
