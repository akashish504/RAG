"""PDF parser using pdfplumber.

Install with: ``pip install 'dalberg-mcp[parsers]'``. If pdfplumber is missing,
``parse`` raises ``NotImplementedError`` with installation instructions rather
than failing silently with empty output.

All pages are concatenated into a single section with no heading so that the
downstream chunker (ResumeChunker or ParentChildChunker) receives the full
document text as one block. This avoids artificial page-boundary splits that
would break job entries spanning two pages and produce meaningless "Page N"
section labels in OpenSearch.
"""

from __future__ import annotations

import io

from pipeline.embedding_pipeline.parser.base import ParsedDocument, ParsedSection


class PDFParser:
    name = "pdf"
    extensions = (".pdf",)

    def parse(self, body: bytes, *, key: str = "") -> ParsedDocument:
        try:
            import pdfplumber
        except ImportError as exc:
            msg = (
                "PDF parsing requires pdfplumber. Install with: "
                "pip install 'dalberg-mcp[parsers]'"
            )
            raise NotImplementedError(msg) from exc

        pages: list[str] = []
        with pdfplumber.open(io.BytesIO(body)) as pdf:
            for page in pdf.pages:
                text = (page.extract_text() or "").strip()
                if text:
                    pages.append(text)

        if not pages:
            return ParsedDocument(
                sections=[],
                parser_name=self.name,
                metadata={"page_count": 0},
            )

        full_text = "\n\n".join(pages)
        return ParsedDocument(
            sections=[
                ParsedSection(text=full_text, heading=None, level=0, section_path=[])
            ],
            parser_name=self.name,
            metadata={"page_count": len(pages)},
        )
