from pipeline.embedding_pipeline.parser.text import TextParser


def test_text_parser_splits_on_markdown_headings() -> None:
    body = b"""# Profile

Lead consultant focused on health systems.

## Experience

Worked on multiple Dalberg engagements.

## Education

PhD in Public Health.
"""
    parsed = TextParser().parse(body)
    headings = [s.heading for s in parsed.sections]
    assert headings == ["Profile", "Experience", "Education"]
    assert parsed.sections[1].text.startswith("Worked on")
    assert parsed.sections[1].section_path == ["Profile", "Experience"]


def test_text_parser_falls_back_to_single_section_for_flat_text() -> None:
    parsed = TextParser().parse(b"just some flat content without headings")
    assert len(parsed.sections) == 1
    assert parsed.sections[0].heading is None


def test_text_parser_detects_all_caps_heading() -> None:
    body = b"PROFILE\n\nA short bio.\n\nEXPERIENCE\n\nDetails here."
    parsed = TextParser().parse(body)
    headings = [s.heading for s in parsed.sections]
    assert headings == ["PROFILE", "EXPERIENCE"]
