"""Document parser layer.

Turns raw bytes into a structured ``ParsedDocument`` (sections with optional
headings) for downstream chunkers.

- **Primary path:** ``TextParser`` for ``.txt`` / ``.md`` produced by ingestion.
- **Optional:** ``PDFParser``, ``DOCXParser``, ``PPTXParser`` (install
  ``dalberg-mcp[parsers]``) for binaries when a table enables those extensions.

Selection is by S3 object extension via ``ParserRegistry``. Which extensions
appear in production is governed by ``config/tables.yaml`` ``supported_extensions``.
"""

from pipeline.embedding_pipeline.parser.base import ParsedDocument, ParsedSection, Parser
from pipeline.embedding_pipeline.parser.docx import DOCXParser
from pipeline.embedding_pipeline.parser.pdf import PDFParser
from pipeline.embedding_pipeline.parser.pptx import PPTXParser
from pipeline.embedding_pipeline.parser.registry import ParserRegistry, default_parsers
from pipeline.embedding_pipeline.parser.text import TextParser

__all__ = [
    "DOCXParser",
    "ParsedDocument",
    "ParsedSection",
    "PDFParser",
    "Parser",
    "ParserRegistry",
    "PPTXParser",
    "TextParser",
    "default_parsers",
]
