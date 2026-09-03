"""Parser registry: maps file extensions to Parser implementations.

Includes:

- ``TextParser`` for ``.txt`` / ``.md`` — always available (primary path once
  ingestion writes ``.txt`` to S3).
- ``PDFParser``, ``DOCXParser``, ``PPTXParser`` — optional; require
  ``pip install 'dalberg-mcp[parsers]'``. Useful for ad‑hoc binaries, staging,
  or if you bypass text-only ingestion temporarily.

Tables still control which extensions are **accepted** via
``supported_extensions`` in ``config/tables.yaml`` (default ``.txt`` only).
Adding ``.pdf`` to a table and installing the ``parsers`` extra enables those
objects without changing reader code.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from pipeline.embedding_pipeline.parser.base import Parser
from pipeline.embedding_pipeline.parser.docx import DOCXParser
from pipeline.embedding_pipeline.parser.pdf import PDFParser
from pipeline.embedding_pipeline.parser.pptx import PPTXParser
from pipeline.embedding_pipeline.parser.text import TextParser


class ParserRegistry:
    """Lookup table from file extension to Parser."""

    def __init__(self, *, parsers: list[Parser] | None = None) -> None:
        self._by_extension: dict[str, Parser] = {}
        for parser in parsers or default_parsers():
            self.register(parser)

    def register(self, parser: Parser) -> None:
        for ext in parser.extensions:
            self._by_extension[ext.lower()] = parser

    def get_for_key(self, key: str) -> Parser:
        ext = PurePosixPath(key).suffix.lower()
        if ext in self._by_extension:
            return self._by_extension[ext]
        if not ext and ".txt" in self._by_extension:
            return self._by_extension[".txt"]
        msg = (
            f"No parser registered for extension {ext!r} (key={key!r}). "
            "Register a parser or add the extension under supported_extensions. "
            "For PDF/DOCX/PPTX install optional deps: pip install 'dalberg-mcp[parsers]'."
        )
        raise KeyError(msg)

    def supported_extensions(self) -> list[str]:
        return sorted(self._by_extension.keys())


def default_parsers() -> list[Parser]:
    """Built-in parsers: text always; binary parsers need ``[parsers]`` deps."""

    return [TextParser(), PDFParser(), DOCXParser(), PPTXParser()]
