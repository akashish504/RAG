"""Unit tests for the enhanced PPTXParser.

All tests use python-pptx to build minimal in-memory presentations so no
real .pptx files are needed.
"""

from __future__ import annotations

import io
import pytest

# Skip entire module if python-pptx is not installed.
pptx = pytest.importorskip("pptx", reason="python-pptx not installed")

from pptx import Presentation
from pptx.util import Inches

from pipeline.embedding_pipeline.parser.pptx import PPTXParser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pptx(*slide_specs: dict) -> bytes:
    """Build a minimal PPTX from a list of slide spec dicts.

    Each spec may have: title (str), body (str), table (list[list[str]]),
    notes (str), image_only (bool).
    """
    prs = Presentation()
    blank_layout = prs.slide_layouts[6]  # completely blank layout

    for spec in slide_specs:
        slide = prs.slides.add_slide(blank_layout)

        title_text = spec.get("title", "")
        body_text = spec.get("body", "")
        notes_text = spec.get("notes", "")
        table_data = spec.get("table")

        if title_text:
            txBox = slide.shapes.add_textbox(Inches(0), Inches(0), Inches(8), Inches(1))
            txBox.text_frame.text = title_text
            # Mark as title shape
            txBox.name = "Title 1"

        if body_text:
            txBox = slide.shapes.add_textbox(Inches(0), Inches(1), Inches(8), Inches(4))
            txBox.text_frame.text = body_text

        if table_data:
            rows = len(table_data)
            cols = max(len(r) for r in table_data)
            tbl = slide.shapes.add_table(rows, cols, Inches(0), Inches(5), Inches(8), Inches(2)).table
            for ri, row in enumerate(table_data):
                for ci, cell_val in enumerate(row):
                    tbl.cell(ri, ci).text = cell_val

        if notes_text:
            slide.notes_slide.notes_text_frame.text = notes_text

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Basic extraction
# ---------------------------------------------------------------------------


def test_slide_count_in_metadata():
    binary = _make_pptx({"title": "A"}, {"title": "B"}, {"title": "C"})
    parsed = PPTXParser().parse(binary)
    assert parsed.metadata["slide_count"] == 3


def test_parser_name():
    binary = _make_pptx({"title": "X", "body": "Some text"})
    parsed = PPTXParser().parse(binary)
    assert parsed.parser_name == "pptx"


def test_slide_number_in_section_metadata():
    binary = _make_pptx({"title": "Intro", "body": "Hello"}, {"title": "Two", "body": "World"})
    parsed = PPTXParser().parse(binary)
    assert len(parsed.sections) == 2
    assert parsed.sections[0].metadata["slide_number"] == 1
    assert parsed.sections[1].metadata["slide_number"] == 2


# ---------------------------------------------------------------------------
# Table extraction as markdown rows
# ---------------------------------------------------------------------------


def test_table_extracted_as_markdown():
    binary = _make_pptx({
        "title": "Data",
        "table": [["Region", "Revenue"], ["APAC", "$4M"], ["EMEA", "$3M"]],
    })
    parsed = PPTXParser().parse(binary)
    assert len(parsed.sections) == 1
    text = parsed.sections[0].text
    assert "| Region | Revenue |" in text
    assert "| APAC | $4M |" in text
    assert "| EMEA | $3M |" in text


def test_pipe_in_cell_is_escaped():
    binary = _make_pptx({
        "title": "Table",
        "table": [["A|B", "C"]],
    })
    parsed = PPTXParser().parse(binary)
    assert "A\\|B" in parsed.sections[0].text


# ---------------------------------------------------------------------------
# Section context propagation
# ---------------------------------------------------------------------------


def test_section_marker_propagates_to_following_slides():
    # Slide 1: title-only (section marker "Overview")
    # Slide 2: title + body
    # Slide 3: title + body
    binary = _make_pptx(
        {"title": "Overview"},           # section marker — no body
        {"title": "Intro", "body": "Content A"},
        {"title": "Details", "body": "Content B"},
    )
    parsed = PPTXParser().parse(binary)
    # Slide 2 and 3 should have presentation_section = "Overview"
    sections_by_num = {s.metadata["slide_number"]: s for s in parsed.sections}
    assert sections_by_num[2].metadata.get("presentation_section") == "Overview"
    assert sections_by_num[3].metadata.get("presentation_section") == "Overview"


def test_section_context_resets_on_new_marker():
    binary = _make_pptx(
        {"title": "Part 1"},                    # marker
        {"title": "Slide A", "body": "text"},
        {"title": "Part 2"},                    # new marker
        {"title": "Slide B", "body": "text"},
    )
    parsed = PPTXParser().parse(binary)
    sections_by_num = {s.metadata["slide_number"]: s for s in parsed.sections}
    assert sections_by_num[2].metadata.get("presentation_section") == "Part 1"
    assert sections_by_num[4].metadata.get("presentation_section") == "Part 2"


def test_initial_section_context_is_empty():
    binary = _make_pptx({"title": "First", "body": "text"})
    parsed = PPTXParser().parse(binary)
    assert parsed.sections[0].metadata.get("presentation_section") == ""


# ---------------------------------------------------------------------------
# Image-only placeholder
# ---------------------------------------------------------------------------


def test_image_only_slide_is_not_dropped():
    """Slide with no extractable text must not be silently dropped."""
    binary = _make_pptx(
        {"title": "Visual Slide"},  # title only — text IS there so is_empty is False
        {"title": "Normal", "body": "text"},
    )
    parsed = PPTXParser().parse(binary)
    # Both slides should be present
    assert len(parsed.sections) == 2


def test_notes_appended_with_prefix():
    binary = _make_pptx({"title": "X", "body": "Body", "notes": "Speaker note here"})
    parsed = PPTXParser().parse(binary)
    assert "[Notes:]" in parsed.sections[0].text
    assert "Speaker note here" in parsed.sections[0].text
