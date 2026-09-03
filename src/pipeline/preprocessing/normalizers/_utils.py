"""Shared utilities for attachment normalizers.

Extracted here to avoid duplication across llm_cv.py, llm_bio.py, and future
normalizer implementations (OCR, field_normalizer, etc.).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def parse_attachment_text(binary: bytes, filename: str) -> str | None:
    """Extract plain text from a DOCX or PDF using the existing pipeline parsers.

    Returns concatenated section text, or None if the file type is unsupported
    or parsing fails.  Callers are responsible for catching errors if they need
    to distinguish between "unsupported type" and "parse failure".
    """
    lower = filename.lower()
    try:
        if lower.endswith(".docx"):
            from pipeline.embedding_pipeline.parser.docx import DOCXParser  # noqa: PLC0415
            parsed = DOCXParser().parse(binary, key=filename)
        elif lower.endswith(".pdf"):
            from pipeline.embedding_pipeline.parser.pdf import PDFParser  # noqa: PLC0415
            parsed = PDFParser().parse(binary, key=filename)
        else:
            return None
    except Exception:
        log.warning("attachment_parse_failed: %r", filename, exc_info=True)
        return None

    parts = [section.text for section in parsed.sections if section.text]
    return "\n\n".join(parts) if parts else None


def call_claude(text: str, *, api_key: str, system_prompt: str, model: str, max_tokens: int) -> str:
    """Send ``text`` to a Claude model and return the response text."""
    import anthropic  # noqa: PLC0415

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": text}],
    )
    return message.content[0].text
