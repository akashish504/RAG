"""Unit tests for PPTXSlideChunker.

Tests cover all three dispatch paths:
  A — normalized .txt with '## Slide N: Title' headings (TextParser output)
  B — raw PPTX with slide_number in section metadata (PPTXParser output)
  C — non-slide content (delegates to ParentChildChunker)
"""

from __future__ import annotations


from pipeline.embedding_pipeline.chunker.base import ChunkingConfig
from pipeline.embedding_pipeline.chunker.pptx_slide import PPTXSlideChunker, _parse_slide_number
from pipeline.embedding_pipeline.models import ChunkType, Document, SourceMetadata
from pipeline.embedding_pipeline.parser.base import ParsedDocument, ParsedSection


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _config(child_max=150, child_min=20, child_overlap=0) -> ChunkingConfig:
    return ChunkingConfig(
        parent_max_tokens=800,
        parent_overlap_tokens=100,
        child_max_tokens=child_max,
        child_min_tokens=child_min,
        child_overlap_tokens=child_overlap,
    )


def _doc(primary_key: str = "doc-001") -> Document:
    return Document(
        document_hash="a" * 64,
        source=SourceMetadata(
            s3_bucket="test-bucket",
            s3_key=f"raw/knowledge_library/{primary_key}/attachment/file.pptx",
            table_name="knowledge_library",
            primary_key=primary_key,
            column_name="Attachment",
            filename="file.pptx",
        ),
    )


def _text_slide_section(slide_num: int, title: str, body: str = "") -> ParsedSection:
    """Simulates a TextParser section from ## Slide N: Title output."""
    return ParsedSection(
        heading=f"Slide {slide_num}: {title}",
        text=body,
        level=2,
        section_path=[f"Slide {slide_num}: {title}"],
        metadata={},
    )


def _pptx_slide_section(slide_num: int, title: str, body: str = "") -> ParsedSection:
    """Simulates a PPTXParser section with slide_number in metadata."""
    return ParsedSection(
        heading=title,
        text=body,
        level=1,
        section_path=[title],
        metadata={"slide_number": slide_num, "presentation_section": "Intro"},
    )


def _parsed_text(sections: list[ParsedSection]) -> ParsedDocument:
    return ParsedDocument(sections=sections, parser_name="text")


def _parsed_pptx(sections: list[ParsedSection]) -> ParsedDocument:
    return ParsedDocument(
        sections=sections,
        parser_name="pptx",
        metadata={"slide_count": len(sections)},
    )


chunker = PPTXSlideChunker()


# ---------------------------------------------------------------------------
# _parse_slide_number helper
# ---------------------------------------------------------------------------


def test_parse_slide_number_matches():
    assert _parse_slide_number("Slide 1: Introduction") == 1
    assert _parse_slide_number("slide 12: data") == 12
    assert _parse_slide_number("SLIDE 3") == 3


def test_parse_slide_number_returns_none_for_non_slide():
    assert _parse_slide_number("Introduction") is None
    assert _parse_slide_number("") is None
    assert _parse_slide_number(None) is None


# ---------------------------------------------------------------------------
# Path A — normalized .txt with ## Slide N: headings
# ---------------------------------------------------------------------------


def test_path_a_one_parent_per_slide():
    sections = [
        _text_slide_section(1, "Introduction", "Hello world"),
        _text_slide_section(2, "Data", "Some data here"),
        _text_slide_section(3, "Conclusion", "Wrap up"),
    ]
    chunks = chunker.chunk(document=_doc(), parsed=_parsed_text(sections), config=_config())
    parents = [c for c in chunks if c.chunk_type == ChunkType.PARENT]
    assert len(parents) == 3


def test_path_a_slide_number_in_metadata():
    sections = [_text_slide_section(5, "Results", "Key findings here")]
    chunks = chunker.chunk(document=_doc(), parsed=_parsed_text(sections), config=_config())
    parent = next(c for c in chunks if c.chunk_type == ChunkType.PARENT)
    assert parent.metadata["slide_number"] == 5
    assert parent.metadata["slide_title"] == "Slide 5: Results"


def test_path_a_at_least_one_child_per_parent():
    sections = [_text_slide_section(1, "Empty", "")]
    chunks = chunker.chunk(document=_doc(), parsed=_parsed_text(sections), config=_config())
    parents = [c for c in chunks if c.chunk_type == ChunkType.PARENT]
    children = [c for c in chunks if c.chunk_type == ChunkType.CHILD]
    assert len(parents) >= 1
    assert len(children) >= 1


def test_path_a_children_within_token_limit():
    long_body = ("A bullet point with some content. " * 20).strip()
    sections = [_text_slide_section(1, "Dense Slide", long_body)]
    chunks = chunker.chunk(document=_doc(), parsed=_parsed_text(sections), config=_config(child_max=50))
    children = [c for c in chunks if c.chunk_type == ChunkType.CHILD]
    from pipeline.embedding_pipeline.chunker.tokens import count_tokens
    for child in children:
        assert count_tokens(child.text) <= 55, f"child too long: {count_tokens(child.text)}"


def test_path_a_child_links_to_parent():
    sections = [_text_slide_section(1, "Slide", "Some text")]
    chunks = chunker.chunk(document=_doc(), parsed=_parsed_text(sections), config=_config())
    parent = next(c for c in chunks if c.chunk_type == ChunkType.PARENT)
    children = [c for c in chunks if c.chunk_type == ChunkType.CHILD]
    for child in children:
        assert child.parent_chunk_id == parent.chunk_id


def test_path_a_strategy_tag():
    sections = [_text_slide_section(1, "X", "text")]
    chunks = chunker.chunk(document=_doc(), parsed=_parsed_text(sections), config=_config())
    for chunk in chunks:
        assert chunk.metadata.get("strategy") == "pptx_slide"


# ---------------------------------------------------------------------------
# Path B — raw PPTX sections with slide_number in metadata
# ---------------------------------------------------------------------------


def test_path_b_one_parent_per_slide():
    sections = [
        _pptx_slide_section(1, "Introduction", "Hello"),
        _pptx_slide_section(2, "Data", "Table content"),
    ]
    chunks = chunker.chunk(document=_doc(), parsed=_parsed_pptx(sections), config=_config())
    parents = [c for c in chunks if c.chunk_type == ChunkType.PARENT]
    assert len(parents) == 2


def test_path_b_slide_metadata_preserved():
    sections = [_pptx_slide_section(7, "Results", "findings")]
    chunks = chunker.chunk(document=_doc(), parsed=_parsed_pptx(sections), config=_config())
    parent = next(c for c in chunks if c.chunk_type == ChunkType.PARENT)
    assert parent.metadata["slide_number"] == 7
    assert parent.metadata["slide_title"] == "Results"
    assert parent.metadata["presentation_section"] == "Intro"


def test_path_b_at_least_one_child_per_parent():
    sections = [_pptx_slide_section(1, "Slide", "")]
    chunks = chunker.chunk(document=_doc(), parsed=_parsed_pptx(sections), config=_config())
    children = [c for c in chunks if c.chunk_type == ChunkType.CHILD]
    assert len(children) >= 1


def test_path_b_slide_count_in_metadata():
    sections = [_pptx_slide_section(i, f"Slide {i}", "text") for i in range(1, 6)]
    chunks = chunker.chunk(document=_doc(), parsed=_parsed_pptx(sections), config=_config())
    parents = [c for c in chunks if c.chunk_type == ChunkType.PARENT]
    for parent in parents:
        assert parent.metadata["slide_count"] == 5


# ---------------------------------------------------------------------------
# Path C — non-slide content delegates to ParentChildChunker
# ---------------------------------------------------------------------------


def test_path_c_non_slide_uses_parent_child():
    sections = [
        ParsedSection(heading="Introduction", text="This is not a slide.", level=1, section_path=["Introduction"]),
        ParsedSection(heading="Methods", text="We used qualitative methods.", level=1, section_path=["Methods"]),
    ]
    parsed = ParsedDocument(sections=sections, parser_name="text")
    chunks = chunker.chunk(document=_doc(), parsed=parsed, config=_config())
    parents = [c for c in chunks if c.chunk_type == ChunkType.PARENT]
    # Should still produce parent chunks (parent_child strategy)
    assert len(parents) >= 1
    # Strategy tag should be parent_child for delegated content
    for chunk in chunks:
        assert chunk.metadata.get("strategy") in ("parent_child", "pptx_slide")


# ---------------------------------------------------------------------------
# Notes block split
# ---------------------------------------------------------------------------


def test_notes_block_becomes_separate_child():
    body = "Main bullet\n[Notes:] Speaker explanation here"
    sections = [_text_slide_section(1, "Slide", body)]
    chunks = chunker.chunk(document=_doc(), parsed=_parsed_text(sections), config=_config())
    children = [c for c in chunks if c.chunk_type == ChunkType.CHILD]
    notes_children = [c for c in children if c.text.startswith("[Notes:]")]
    assert len(notes_children) == 1


# ---------------------------------------------------------------------------
# Chunk IDs are unique
# ---------------------------------------------------------------------------


def test_all_chunk_ids_unique():
    sections = [_text_slide_section(i, f"Slide {i}", "content " * 10) for i in range(1, 6)]
    chunks = chunker.chunk(document=_doc(), parsed=_parsed_text(sections), config=_config())
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids)), "Duplicate chunk IDs detected"


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_pptx_slide_registered_in_default_registry():
    from pipeline.embedding_pipeline.chunker.registry import default_chunker_registry
    registry = default_chunker_registry()
    assert "pptx_slide" in registry.names()
    assert isinstance(registry.get("pptx_slide"), PPTXSlideChunker)


# ---------------------------------------------------------------------------
# New behaviours: table-atomic blocks, slide-title embed prefix, summary vectors
# ---------------------------------------------------------------------------


def _summary_doc(doc_role: str, text: str) -> Document:
    return Document(
        document_hash="b" * 64,
        text=text,
        source=SourceMetadata(
            s3_bucket="b", s3_key="raw/d.quals/p1/__record_summary.txt",
            table_name="d_quals", primary_key="p1", column_name="", filename="x.txt",
            doc_role=doc_role,
        ),
    )


def test_table_stays_atomic_in_one_child():
    body = "- intro bullet\n| Item | USD |\n|---|---|\n| Phase 1 | 1000 |\n| Phase 2 | 2500 |"
    chunks = chunker.chunk(
        document=_doc(), parsed=_parsed_text([_text_slide_section(1, "Costs", body)]),
        config=_config(child_max=150),
    )
    children = [c for c in chunks if c.chunk_type is ChunkType.CHILD]
    table_children = [c for c in children if "| Phase 1 | 1000 |" in c.text]
    assert len(table_children) == 1
    # The whole table (both rows + header) is in that single child.
    t = table_children[0].text
    assert "| Item | USD |" in t and "| Phase 2 | 2500 |" in t


def test_child_embed_text_has_slide_prefix_stored_text_is_raw():
    chunks = chunker.chunk(
        document=_doc(), parsed=_parsed_text([_text_slide_section(7, "Market sizing", "- 23% CAGR")]),
        config=_config(child_max=150, child_min=1),
    )
    child = next(c for c in chunks if c.chunk_type is ChunkType.CHILD)
    assert child.text == "- 23% CAGR"                       # stored/cited = raw block
    assert child.embed_text.startswith("Slide 7: Market sizing\n\n")  # embedded = prefixed
    assert "- 23% CAGR" in child.embed_text


def test_record_summary_is_single_vector():
    doc = _summary_doc("record_summary", "DOCUMENT_SUMMARY: A health project in Kenya for Gates.")
    chunks = chunker.chunk(
        document=doc, parsed=_parsed_text([_text_slide_section(1, "x", "ignored")]),
        config=_config(),
    )
    assert len(chunks) == 1
    assert chunks[0].chunk_type is ChunkType.CHILD          # embeddable
    assert chunks[0].metadata["doc_role"] == "record_summary"
    assert "Kenya for Gates" in chunks[0].text


def test_deck_summary_preamble_is_single_vector():
    preamble = ParsedSection(
        heading="", text="DOCUMENT_SUMMARY: A strategy deck for AfDB.", level=1, section_path=[],
    )
    chunks = chunker.chunk(
        document=_doc(),
        parsed=_parsed_text([preamble, _text_slide_section(1, "Intro", "- bullet")]),
        config=_config(child_min=1),
    )
    deck_sum = [c for c in chunks if c.metadata.get("doc_role") == "deck_summary"]
    assert len(deck_sum) == 1
    assert "AfDB" in deck_sum[0].text
    # The actual slide is still chunked normally.
    assert any(c.chunk_type is ChunkType.PARENT for c in chunks)
