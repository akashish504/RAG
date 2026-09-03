"""Resume-aware chunking strategy.

This strategy is tuned for the ``profile`` table where ``.txt`` files are
resumes/CVs. It does two things:

1. **Section-wise chunking.** Each canonical resume section (Summary, Skills,
   Experience, Education, Projects, Certifications, ...) becomes one parent
   chunk that carries the full section as context. The original heading is
   preserved on the chunk and a normalized ``section_canonical`` field is
   added (e.g. "Senior Engineer Experience" -> ``experience``) so retrieval
   and analytics can group across resumes regardless of how each author
   styled their headings.

2. **Parent-child chunking for list-like sections.** Sections that naturally
   contain a list of independent entries (Experience, Projects, Education,
   Publications, Volunteer) are split into one *child* chunk per entry. Each
   entry stays a logical unit: a single job, project, or degree is never
   split across child chunks. Children carry ``parent_chunk_id`` so retrieval
   can fetch the full parent section text for grounded responses.

Design notes:

- The strategy never breaks a logical entry. If a single entry exceeds the
  configured ``child_max_tokens``, it falls back to overlapping token windows
  but those windows are tagged with the same ``entry_index`` so the entry can
  still be reconstructed. This protects the constraint
  "do not split a single job entry across chunks" in the common case while
  remaining safe for unusually verbose entries.
- Token-window splitting at the *parent* level is intentionally not used for
  list sections. The parent is conceptually the section, however long.
- For non-list sections that exceed ``parent_max_tokens``, the strategy
  falls back to overlapping token windows for the parent (same behavior as
  ``ParentChildChunker``), which keeps retrieval working for unusually
  verbose summaries or skill matrices.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pipeline.common.ids import chunk_id as build_chunk_id
from pipeline.embedding_pipeline.chunker.base import ChunkingConfig
from pipeline.embedding_pipeline.chunker.tokens import count_tokens, split_to_token_window
from pipeline.embedding_pipeline.models import Chunk, ChunkType, Document
from pipeline.embedding_pipeline.parser.base import ParsedDocument, ParsedSection

# ---------------------------------------------------------------------------
# Section name canonicalisation
# ---------------------------------------------------------------------------

# Canonical section name -> ordered list of normalized aliases.
# Aliases are matched case-insensitively against the section heading after
# stripping punctuation and whitespace.
_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "summary": (
        "profile",
        "summary",
        "professional summary",
        "personal summary",
        "objective",
        "career objective",
        "about",
        "about me",
    ),
    "skills": (
        "skills",
        "technical skills",
        "core competencies",
        "competencies",
        "areas of expertise",
        "key skills",
    ),
    "experience": (
        "experience",
        "work experience",
        "professional experience",
        "employment",
        "employment history",
        "employment record",
        "work history",
        "career history",
        "professional background",
        "consulting experience",
    ),
    "education": (
        "education",
        "academic background",
        "qualifications",
        "academic qualifications",
        "academic credentials",
    ),
    "projects": (
        "projects",
        "selected projects",
        "key projects",
        "personal projects",
    ),
    "certifications": (
        "certifications",
        "certificates",
        "licenses",
        "licences",
    ),
    "awards": (
        "awards",
        "achievements",
        "honors",
        "honours",
        "recognition",
    ),
    "languages": (
        "languages",
        "language proficiency",
    ),
    "publications": (
        "publications",
        "papers",
        "research",
    ),
    "volunteer": (
        "volunteer",
        "volunteering",
        "community involvement",
        "community service",
    ),
}

# Sections whose body is treated as a list of entries to be split into
# children. Education, Experience, etc. typically have multiple distinct
# entries; Skills is treated as a single block (children are token windows
# only if the section is huge).
_LIST_SECTIONS: frozenset[str] = frozenset(
    {"experience", "education", "projects", "publications", "volunteer"}
)

# Short tokenisable headings that hint at "this is a heading" even when the
# underlying ``.txt`` did not use markdown ``#`` syntax. Used as a fallback
# inside Education/Experience-like sections that came in with trailing labels.
_HEADING_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def canonical_section(heading: str | None) -> str:
    """Map a raw heading string to one of the canonical section names.

    Returns the canonical name (e.g. ``"experience"``) or ``"other"`` if the
    heading does not match any known alias. Empty/None headings yield
    ``"unknown"``.
    """

    if not heading:
        return "unknown"
    normalized = _normalize(heading)
    if not normalized:
        return "unknown"
    for canonical, aliases in _SECTION_ALIASES.items():
        for alias in aliases:
            if normalized == _normalize(alias):
                return canonical
    return "other"


def _normalize(text: str) -> str:
    return _HEADING_PUNCT_RE.sub(" ", text.lower()).strip()


# ---------------------------------------------------------------------------
# Entry splitting for list-like sections
# ---------------------------------------------------------------------------

# Heuristics for spotting the *start* of a new entry inside a list section.
# We deliberately keep these conservative: if nothing matches, we fall back to
# splitting on blank lines (paragraphs).

_DATE_RANGE_RE = re.compile(
    r"""
    \b(
        (jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|
         jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|
         dec(?:ember)?)?\s*\d{4}
        \s*[-\u2013\u2014to/]+\s*
        (jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|
         jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|
         dec(?:ember)?)?\s*(\d{4}|present|current|now)
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PIPE_HEADER_RE = re.compile(r"^[^\n|]+(\s*\|\s*[^\n|]+){1,4}$")
_BLANK_LINE_SPLIT_RE = re.compile(r"\n\s*\n")


def split_section_into_entries(text: str) -> list[str]:
    """Split a list-section body into individual entry blocks.

    Strategy:

    - Split into paragraphs separated by blank lines.
    - Walk paragraphs left-to-right. A paragraph starts a new entry if its
      first non-empty line contains a date range (e.g. ``Jan 2020 - Present``)
      or looks like a pipe-separated header (e.g.
      ``Senior Engineer | Acme | Jan 2020 - Present``).
    - Otherwise, append it to the previous entry as continuation. This keeps
      multi-paragraph entries intact (e.g. a job description with bullets in
      one paragraph and an outcome in another).
    - If the section is a single dense block (no blank lines) but individual
      lines look like entry starts, fall back to line-level splitting. This
      handles serialized table-format CVs where rows have no blank-line gaps.
    - If no structural markers are found anywhere, fall back to one entry per
      paragraph.
    """

    body = (text or "").strip()
    if not body:
        return []

    paragraphs = [p.strip() for p in _BLANK_LINE_SPLIT_RE.split(body) if p.strip()]
    if not paragraphs:
        return []

    # For single-paragraph dense text (no blank lines), try line-level splitting.
    # This handles table-format CVs where each job row is a single line.
    if len(paragraphs) == 1:
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        line_markers = [_paragraph_starts_entry(ln) for ln in lines]
        if sum(line_markers) > 1:
            paragraphs = lines

    markers = [_paragraph_starts_entry(p) for p in paragraphs]
    if not any(markers):
        # No structural markers anywhere — fall back to one entry per
        # paragraph. Resumes commonly separate distinct entries by blank
        # lines even when no date/pipe header is present.
        return paragraphs

    entries: list[list[str]] = []
    for paragraph, is_marker in zip(paragraphs, markers):
        if is_marker:
            entries.append([paragraph])
        elif entries:
            entries[-1].append(paragraph)
        else:
            # Preamble before the first marker — keep as its own entry so it
            # is not silently absorbed into entry 0.
            entries.append([paragraph])

    return ["\n\n".join(parts) for parts in entries]


def _paragraph_starts_entry(paragraph: str) -> bool:
    first_line = paragraph.split("\n", 1)[0].strip()
    if not first_line:
        return False
    if _DATE_RANGE_RE.search(first_line):
        return True
    return bool(_PIPE_HEADER_RE.match(first_line))


# ---------------------------------------------------------------------------
# Resume chunker
# ---------------------------------------------------------------------------


class ResumeChunker:
    """Section-wise + parent-child chunker for resume / CV ``.txt`` files."""

    name: str = "resume"
    list_sections: frozenset[str] = _LIST_SECTIONS

    def chunk(
        self,
        *,
        document: Document,
        parsed: ParsedDocument,
        config: ChunkingConfig,
    ) -> list[Chunk]:
        chunks: list[Chunk] = []
        parent_position = 0

        sections = list(parsed.sections) or []
        if not any(s.heading for s in sections):
            # No headings detected — treat the whole document as a single
            # "summary" section so we still produce useful chunks.
            sections = [
                ParsedSection(
                    text=parsed.text,
                    heading=None,
                    level=0,
                    section_path=[],
                )
            ]

        for section in sections:
            if section.is_empty:
                continue

            canonical = canonical_section(section.heading)
            is_list = canonical in self.list_sections
            # For sections with unrecognised headings, auto-detect whether the
            # body contains structured entries (date ranges, pipe headers).
            # If yes, treat it as a list section so we get entry-level children
            # rather than arbitrary token-window children.
            if not is_list and canonical in ("unknown", "other") and section.text:
                probe = split_section_into_entries(section.text)
                if len(probe) > 1:
                    is_list = True
            section_text = self._compose_section_text(section)
            section_tokens = count_tokens(section_text, config.tokenizer)
            if section_tokens == 0:
                continue

            if is_list:
                # Parent is the WHOLE section. List sections are not
                # token-windowed at the parent level so children can stay
                # cleanly anchored to one parent.
                parent = self._build_parent_chunk(
                    document=document,
                    section=section,
                    canonical=canonical,
                    text=section_text,
                    token_count=section_tokens,
                    parent_position=parent_position,
                    is_list_section=True,
                )
                chunks.append(parent)
                chunks.extend(
                    self._build_entry_children(
                        document=document,
                        section=section,
                        canonical=canonical,
                        parent=parent,
                        parent_position=parent_position,
                        config=config,
                    )
                )
                parent_position += 1
                continue

            # Non-list section: standard parent token-window fallback.
            parent_texts = self._split_section_into_parents(
                text=section_text,
                section_tokens=section_tokens,
                config=config,
            )
            for parent_text in parent_texts:
                parent_tokens = count_tokens(parent_text, config.tokenizer)
                parent = self._build_parent_chunk(
                    document=document,
                    section=section,
                    canonical=canonical,
                    text=parent_text,
                    token_count=parent_tokens,
                    parent_position=parent_position,
                    is_list_section=False,
                )
                chunks.append(parent)
                chunks.extend(
                    self._build_token_children(
                        document=document,
                        section=section,
                        canonical=canonical,
                        parent=parent,
                        parent_position=parent_position,
                        config=config,
                    )
                )
                parent_position += 1

        return chunks

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compose_section_text(section: ParsedSection) -> str:
        if section.heading and section.text:
            return f"{section.heading}\n\n{section.text}"
        if section.heading:
            return section.heading
        return section.text

    @staticmethod
    def _split_section_into_parents(
        *,
        text: str,
        section_tokens: int,
        config: ChunkingConfig,
    ) -> list[str]:
        if section_tokens <= config.parent_max_tokens:
            return [text]
        return split_to_token_window(
            text,
            max_tokens=config.parent_max_tokens,
            overlap=config.parent_overlap_tokens,
            encoding_name=config.tokenizer,
        )

    @staticmethod
    def _base_metadata(
        *,
        document: Document,
        section: ParsedSection,
        canonical: str,
        chunk_index: int,
        is_list_section: bool,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "section_title": section.heading,
            "section_canonical": canonical,
            "section_level": section.level,
            "section_path": list(section.section_path),
            "is_list_section": is_list_section,
            "chunk_index": chunk_index,
            "table_name": document.source.table_name,
            "document_id": document.source.primary_key,
            "s3_path": document.source.s3_key,
            "strategy": "resume",
        }
        for key, value in section.metadata.items():
            metadata.setdefault(key, value)
        return metadata

    def _build_parent_chunk(
        self,
        *,
        document: Document,
        section: ParsedSection,
        canonical: str,
        text: str,
        token_count: int,
        parent_position: int,
        is_list_section: bool,
    ) -> Chunk:
        cid = build_chunk_id(
            document_hash_value=document.document_hash,
            chunk_type=ChunkType.PARENT,
            parent_position=parent_position,
        )
        metadata = self._base_metadata(
            document=document,
            section=section,
            canonical=canonical,
            chunk_index=parent_position,
            is_list_section=is_list_section,
        )
        return Chunk(
            chunk_id=cid,
            chunk_type=ChunkType.PARENT,
            text=text,
            token_count=token_count,
            position=parent_position,
            document_hash=document.document_hash,
            source=document.source,
            parent_chunk_id=None,
            metadata=metadata,
        )

    def _build_entry_children(
        self,
        *,
        document: Document,
        section: ParsedSection,
        canonical: str,
        parent: Chunk,
        parent_position: int,
        config: ChunkingConfig,
    ) -> list[Chunk]:
        entries = split_section_into_entries(section.text)
        if not entries:
            return []

        # Prefix every entry child with its section heading so the embedding
        # captures section context ("Professional Experience:\nJan 2019…")
        # rather than a decontextualised date/title line alone.
        heading_prefix = (section.heading or canonical.title()) + ":\n"

        children: list[Chunk] = []
        child_position = 0
        for entry_index, entry_text in enumerate(entries):
            entry_text = entry_text.strip()
            if not entry_text:
                continue
            embed_text = heading_prefix + entry_text
            tokens = count_tokens(embed_text, config.tokenizer)
            if tokens == 0:
                continue
            if tokens < config.child_min_tokens and children:
                # Merge a too-short tail entry into the previous child to
                # avoid emitting near-empty chunks. The merged child stays
                # under one entry boundary because we only merge tails.
                self._merge_tail_into_previous(children[-1], entry_text, config)
                continue

            if tokens <= config.child_max_tokens:
                children.append(
                    self._build_child_chunk(
                        document=document,
                        section=section,
                        canonical=canonical,
                        parent=parent,
                        parent_position=parent_position,
                        child_position=child_position,
                        text=embed_text,
                        token_count=tokens,
                        entry_index=entry_index,
                        sub_index=None,
                    )
                )
                child_position += 1
            else:
                # Single oversized entry: token-window split it but keep all
                # windows tagged with the same ``entry_index`` so retrieval
                # can group them back into one logical entry.
                windows = split_to_token_window(
                    embed_text,
                    max_tokens=config.child_max_tokens,
                    overlap=config.child_overlap_tokens,
                    encoding_name=config.tokenizer,
                )
                for sub_index, window in enumerate(windows):
                    window_tokens = count_tokens(window, config.tokenizer)
                    children.append(
                        self._build_child_chunk(
                            document=document,
                            section=section,
                            canonical=canonical,
                            parent=parent,
                            parent_position=parent_position,
                            child_position=child_position,
                            text=window,
                            token_count=window_tokens,
                            entry_index=entry_index,
                            sub_index=sub_index,
                        )
                    )
                    child_position += 1
        return children

    def _build_token_children(
        self,
        *,
        document: Document,
        section: ParsedSection,
        canonical: str,
        parent: Chunk,
        parent_position: int,
        config: ChunkingConfig,
    ) -> list[Chunk]:
        # For non-list sections (e.g. Summary, Skills) we always produce at
        # least one child so the section text is embedded and findable via KNN.
        # Without a child chunk, parent-only sections are invisible to vector
        # search because only child chunks receive embeddings.
        windows = split_to_token_window(
            parent.text,
            max_tokens=config.child_max_tokens,
            overlap=config.child_overlap_tokens,
            encoding_name=config.tokenizer,
        )
        if not windows:
            return []

        children: list[Chunk] = []
        for child_position, window in enumerate(windows):
            tokens = count_tokens(window, config.tokenizer)
            # Drop a trailing sliver only when it is truly tiny and there is
            # already at least one valid child before it.
            if tokens < config.child_min_tokens and children:
                continue
            children.append(
                self._build_child_chunk(
                    document=document,
                    section=section,
                    canonical=canonical,
                    parent=parent,
                    parent_position=parent_position,
                    child_position=child_position,
                    text=window,
                    token_count=tokens,
                    entry_index=None,
                    sub_index=None,
                )
            )
        return children

    def _build_child_chunk(
        self,
        *,
        document: Document,
        section: ParsedSection,
        canonical: str,
        parent: Chunk,
        parent_position: int,
        child_position: int,
        text: str,
        token_count: int,
        entry_index: int | None,
        sub_index: int | None,
    ) -> Chunk:
        cid = build_chunk_id(
            document_hash_value=document.document_hash,
            chunk_type=ChunkType.CHILD,
            parent_position=parent_position,
            child_position=child_position,
        )
        metadata = self._base_metadata(
            document=document,
            section=section,
            canonical=canonical,
            chunk_index=child_position,
            is_list_section=entry_index is not None,
        )
        metadata["child_index_in_parent"] = child_position
        if entry_index is not None:
            metadata["entry_index"] = entry_index
        if sub_index is not None:
            metadata["entry_sub_index"] = sub_index
        return Chunk(
            chunk_id=cid,
            chunk_type=ChunkType.CHILD,
            text=text,
            token_count=token_count,
            position=child_position,
            document_hash=document.document_hash,
            source=document.source,
            parent_chunk_id=parent.chunk_id,
            metadata=metadata,
        )

    @staticmethod
    def _merge_tail_into_previous(
        previous: Chunk, tail_text: str, config: ChunkingConfig
    ) -> None:
        merged = f"{previous.text}\n\n{tail_text}"
        previous.text = merged
        previous.token_count = count_tokens(merged, config.tokenizer)


__all__: Iterable[str] = ("ResumeChunker", "canonical_section", "split_section_into_entries")
