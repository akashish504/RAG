"""Plain text and markdown parser with lightweight heading detection."""

from __future__ import annotations

import re

from pipeline.embedding_pipeline.parser.base import ParsedDocument, ParsedSection

# Matches a YAML-style frontmatter block at the very start of a file:
#   ---
#   Key: Value
#   ---
# The --- delimiters are stripped; key-value lines remain as plain text so they
# contribute to embeddings and BM25 search.  Parsed pairs go into
# ParsedDocument.metadata for structured access downstream.
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_ALL_CAPS_HEADING = re.compile(r"^[A-Z][A-Z0-9 \-:&,/()]{3,}$")

# Vocabulary of known CV/document section words for title-case heading detection.
# A short title-case line is only treated as a heading if it contains at least one
# of these words — preventing person names and company names from false-matching.
_CV_SECTION_VOCAB: frozenset[str] = frozenset({
    "summary", "profile", "objective", "about", "overview",
    "skills", "competencies", "expertise", "proficiencies", "qualifications",
    "experience", "employment", "work", "career", "history", "record", "professional",
    "education", "academic", "training",
    "projects", "portfolio",
    "certifications", "certificates", "licenses", "credentials",
    "awards", "achievements", "honors", "recognition",
    "languages", "language",
    "publications", "research",
    "volunteer", "volunteering", "community",
    "other", "additional", "interests", "activities", "references",
})

# Matches a short title-case line: starts uppercase, only letters/spaces/hyphens/
# ampersands, 2–60 chars. Lines with digits or punctuation like ( ) | / @ , are
# excluded — those appear in job-entry lines, not section headings.
_TITLE_CASE_HEADING = re.compile(r"^[A-Z][a-zA-Z&\-\s]{1,59}$")


class TextParser:
    """Parser for ``.txt`` and ``.md`` files.

    Splits content along detected headings:

    - Markdown headings (``# Title``, ``## Subtitle``).
    - Short ALL-CAPS lines, treated as level-1 headings (common in CVs and
      consulting decks exported to text).

    When no headings are found, the entire document becomes a single section.
    """

    name = "text"
    extensions = (".txt", ".md", ".markdown")

    def __init__(self, *, encoding: str = "utf-8") -> None:
        self.encoding = encoding

    def parse(self, body: bytes, *, key: str = "") -> ParsedDocument:
        text = body.decode(self.encoding, errors="replace")
        frontmatter: dict[str, str] = {}
        m = _FRONTMATTER_RE.match(text)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    frontmatter[k.strip()] = v.strip()
            # Replace fenced block with just the key-value lines so the metadata
            # content stays in the text for embedding/BM25 without the --- noise.
            text = m.group(1) + "\n\n" + text[m.end():]
        sections = self._split_into_sections(text)
        if not sections:
            sections = [ParsedSection(text=text.strip())]
        return ParsedDocument(sections=sections, parser_name=self.name, metadata=frontmatter)

    def _split_into_sections(self, text: str) -> list[ParsedSection]:
        sections: list[ParsedSection] = []
        current_heading: str | None = None
        current_level = 0
        current_path: list[str] = []
        current_lines: list[str] = []

        def flush() -> None:
            chunk_text = "\n".join(current_lines).strip()
            if chunk_text or current_heading:
                sections.append(
                    ParsedSection(
                        text=chunk_text,
                        heading=current_heading,
                        level=current_level,
                        section_path=list(current_path),
                    )
                )

        for raw_line in text.splitlines():
            heading = self._match_heading(raw_line)
            if heading is not None:
                flush()
                current_lines = []
                level, heading_text = heading
                while len(current_path) >= level:
                    current_path.pop()
                current_path.append(heading_text)
                current_heading = heading_text
                current_level = level
            else:
                current_lines.append(raw_line)

        flush()
        return [s for s in sections if not s.is_empty]

    @staticmethod
    def _match_heading(line: str) -> tuple[int, str] | None:
        stripped = line.strip()
        if not stripped:
            return None

        md = _MARKDOWN_HEADING.match(stripped)
        if md:
            level = len(md.group(1))
            heading = md.group(2).strip()
            return level, heading

        if 4 <= len(stripped) <= 100 and _ALL_CAPS_HEADING.match(stripped):
            return 1, stripped

        # Title-case short lines matching at least one known section vocab word.
        # ≤5 words prevents matching full sentences; vocab guard prevents matching
        # person names ("Alexander Raymond Rubio") or company names ("Dalberg Advisors").
        if _TITLE_CASE_HEADING.match(stripped):
            words = stripped.lower().split()
            if 1 <= len(words) <= 5 and any(w in _CV_SECTION_VOCAB for w in words):
                return 1, stripped

        return None
