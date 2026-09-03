"""Parser protocol and parsed-document data classes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class ParsedSection:
    """A semantically meaningful slice of a document.

    Sections preserve the original structure of the source: a markdown heading
    block, a DOCX heading + body, a PDF page, or a PPTX slide. Each section is
    independently chunkable.
    """

    text: str
    heading: str | None = None
    level: int = 0
    section_path: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip() and not self.heading


@dataclass(slots=True)
class ParsedDocument:
    """Structured view of a single source object."""

    sections: list[ParsedSection] = field(default_factory=list)
    parser_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Flattened text for fallback / search-only consumers."""

        parts: list[str] = []
        for section in self.sections:
            if section.is_empty:
                continue
            if section.heading and section.text:
                parts.append(f"{section.heading}\n{section.text}")
            elif section.heading:
                parts.append(section.heading)
            else:
                parts.append(section.text)
        return "\n\n".join(parts).strip()

    @property
    def headings(self) -> list[str]:
        return [s.heading for s in self.sections if s.heading]


class Parser(Protocol):
    """Any object that turns raw bytes into a structured ParsedDocument."""

    name: str
    extensions: tuple[str, ...]

    def parse(self, body: bytes, *, key: str = "") -> ParsedDocument:
        """Parse bytes for the given object key into a ParsedDocument."""
