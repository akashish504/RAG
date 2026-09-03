"""LLMCVNormalizer — rewrites all CVs to clean, structured plain text.

Parses DOCX/PDF with the existing pipeline parsers, then calls Claude Haiku to
rewrite the content as structured prose with canonical section headings. This
ensures consistent chunk quality regardless of source format (narrative, table,
or hybrid layouts).

Output format (used in the LLM prompt):
  NAME / PROFILE / EXPERIENCE / EDUCATION / LANGUAGES / SKILLS / CERTIFICATIONS
  — headings that the ResumeChunker recognises as canonical sections, enabling
  correct ``section_canonical`` tags and good token distribution per child chunk.
"""

from __future__ import annotations

import logging

from pipeline.preprocessing.normalizers._utils import call_claude, parse_attachment_text

log = logging.getLogger(__name__)

_NORMALIZER_MODEL = "claude-haiku-4-5-20251001"
_MAX_OUTPUT_TOKENS = 4096

_SYSTEM_PROMPT = """\
You are a professional CV formatter. Convert the input CV into clean, structured \
plain text using the section headings listed below. Use ONLY the headings that \
are relevant to the content present in the CV.

Required output structure:

NAME: [Full name of the person]

PROFILE
[Key metadata in plain prose: proposed role, firm, nationality, countries of \
work experience — one sentence or a few lines. Omit if no metadata present.]

EXPERIENCE

[For each role, use this exact layout:]
[Job Title]
[Organisation], [City, Country]
[Date range]

[One-paragraph description of the role followed by key achievements. \
Keep all metrics, numbers, and named outcomes verbatim.]

EDUCATION
[Degree name], [Institution name], [Year]
[Repeat for each qualification]

LANGUAGES
[Language name]: [Proficiency level]
[Repeat for each language]

SKILLS
[List of technical and professional skills mentioned, one per line]

CERTIFICATIONS
[List certifications, publications, awards — one per line]

Rules:
- Output plain text only — no markdown, no JSON, no pipe characters, no tables, \
no bullet symbols (•, -, *)
- Preserve ALL factual information: every role, organisation, date, achievement, \
metric, project name, and credential present in the source
- Use blank lines between sections and between individual entries within a section
- Do not add information not present in the source
- Do not truncate or summarise — include every detail
"""


class LLMCVNormalizer:
    """Normalize CV attachments by rewriting all DOCX/PDF CVs with Claude Haiku.

    - Suitable for ``llm_cv`` in ``airtable_ingestion.yaml`` per-column config.
    - Only DOCX and PDF files are processed; all others pass through (return None).
    - Every CV triggers an LLM call regardless of layout (narrative or table-format).
    - Returns None on any error — ingestion falls back to uploading original binary.
    """

    def __init__(self, *, api_key: str) -> None:
        self._api_key = api_key

    def normalize(self, binary: bytes, filename: str) -> str | None:
        """Return LLM-normalised structured text for all DOCX/PDF CVs, None otherwise."""
        try:
            text = parse_attachment_text(binary, filename)
            if text is None:
                return None

            log.info(
                "llm_cv_normalizer: normalising %r (%d chars)",
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
                "llm_cv_normalizer: error normalising %r, falling back to original",
                filename,
                exc_info=True,
            )
            return None
