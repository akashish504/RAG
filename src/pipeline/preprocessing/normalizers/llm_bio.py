"""LLMBioNormalizer — structures professional bio attachments for clean retrieval.

Bio attachments (e.g. "Bio Attachment" in Dalberg Profiles) are narrative prose
documents written for specific audiences or proposal contexts.  Typical structure:

  Adriana Rueda
  Please reach out for a sector-specific bio…

  Quick Blurb
  Adriana has spent over a decade working at the intersection of social impact…

  BIO for Workshop expertise and Brazil
  Adriana Rueda is a Project Manager at Dalberg Advisors with expertise in
  financial structuring, private sector development…

Unlike CVs, bios are already clean prose — the problem is inconsistent section
naming and lack of extractable metadata (languages, education, skills) that the
embedding pipeline needs for retrieval.

This normalizer:
  1. Parses the attachment with the existing DocxParser or PdfParser.
  2. ALWAYS calls the LLM (no table-format gate) to produce a consistently
     structured plain-text output.
  3. Outputs standard section headings that the ResumeChunker recognises:
     NAME / SUMMARY / EXPERIENCE / EDUCATION / LANGUAGES / SKILLS
  4. Returns the structured text for storage as ``{id}__normalized.txt`` in S3.
  5. Returns None on any error (safe fallback — ingestion uploads original binary).

Output format is designed so that:
  - SUMMARY  → short-form bio, quick blurb, or overview (canonical: "summary")
  - EXPERIENCE → each long-form / context-specific bio variant (canonical: "experience")
  - EDUCATION, LANGUAGES, SKILLS → extracted from bio prose (canonical sections)

This lets the ResumeChunker assign correct ``section_canonical`` values and the
embedding pipeline produce semantically rich child chunks.
"""

from __future__ import annotations

import logging

from pipeline.preprocessing.normalizers._utils import call_claude, parse_attachment_text

log = logging.getLogger(__name__)

_NORMALIZER_MODEL = "claude-haiku-4-5-20251001"
_MAX_OUTPUT_TOKENS = 4096

_SYSTEM_PROMPT = """\
You are a professional bio formatter. Convert the input professional bio into \
clean, structured plain text using the section headings listed below. Use ONLY \
the headings that are relevant to the content actually present in the document.

Required output structure:

NAME: [Full name of the person]

SUMMARY
[The shortest bio or "quick blurb" — verbatim or lightly cleaned. \
This is typically 2-4 sentences describing who the person is and their key expertise.]

EXPERIENCE
[The main long-form bio or role-specific bio. Include the full narrative text. \
If multiple contextual bio variants exist (e.g. "BIO for Workshop expertise and Brazil"), \
include each as a separate paragraph under EXPERIENCE, preceded by a label line such as:]
BIO: [CONTEXT OR PURPOSE IN CAPS]
[Full text of that bio variant]

[Repeat for each bio variant present in the source document]

SKILLS
[Key expertise areas, functional skills, thematic topics, and sector knowledge \
mentioned across all bio variants — extracted as a clean list, one item per line. \
Examples: financial structuring, impact investing, supply chain strategy, \
sustainability reporting, stakeholder engagement]

EDUCATION
[For each qualification:]
[Degree name], [Institution name], [Year if present]

LANGUAGES
[Language name]: [Proficiency level if stated, otherwise "mentioned"]

Rules:
- Output plain text only — no markdown, no JSON, no bullet symbols (•, -, *), \
no pipe characters, no tables
- Preserve ALL factual content: names, organisations, dates, metrics, achievements, \
project names, and any specific technical details
- Use blank lines between sections and between paragraphs within a section
- Do not add, invent, or summarise information not present in the source
- If the document contains a note like "Please reach out for a sector-specific bio", \
omit it from the output — it is not content
- If no distinct bio variants exist, write the full bio under EXPERIENCE
"""


class LLMBioNormalizer:
    """Normalize bio attachments into consistently structured plain text.

    Unlike LLMCVNormalizer, this normalizer does NOT gate on table-format detection —
    bios are always processed since the goal is consistent structure, not just
    fixing table-format encoding issues.

    Suitable for ``llm_bio`` in ``attachment_column_normalizers`` config.
    Only DOCX and PDF files are processed; all others pass through (return None).
    Returns None on any error — ingestion falls back to uploading original binary.
    """

    def __init__(self, *, api_key: str) -> None:
        self._api_key = api_key

    def normalize(self, binary: bytes, filename: str) -> str | None:
        """Return LLM-structured text for bio attachments, None for unsupported types."""
        try:
            text = parse_attachment_text(binary, filename)
            if text is None:
                return None

            log.info(
                "llm_bio_normalizer: normalising bio %r (%d chars)",
                filename,
                len(text),
            )
            return call_claude(
                text,
                api_key=self._api_key,
                system_prompt=_SYSTEM_PROMPT,
                model=_NORMALIZER_MODEL,
                max_tokens=_MAX_OUTPUT_TOKENS,
            )

        except Exception:
            log.warning(
                "llm_bio_normalizer: error normalising %r, falling back to original",
                filename,
                exc_info=True,
            )
            return None
