"""Tests for DOCX parser fallback section detection (no python-docx required)."""

from pipeline.embedding_pipeline.parser.docx import (
    _extract_section_from_pipe_row,
    _split_text_into_sections,
)


class TestExtractSectionFromPipeRow:
    def test_detects_employment_record(self) -> None:
        assert _extract_section_from_pipe_row(
            "12. | Employment Record | Employment Record | Detailed below"
        ) == "Employment Record"

    def test_detects_education(self) -> None:
        assert _extract_section_from_pipe_row(
            "13. | Education | Education | Detailed below"
        ) == "Education"

    def test_detects_publications(self) -> None:
        assert _extract_section_from_pipe_row(
            "11. | Publications | Publications | Listed below"
        ) == "Publications"

    def test_ignores_language_data_row(self) -> None:
        # "Languages" is a personal data field in the WB header table, not a section
        assert (
            _extract_section_from_pipe_row("10. | Languages | Languages | English (Fluent)")
            is None
        )

    def test_ignores_proposed_position_row(self) -> None:
        assert (
            _extract_section_from_pipe_row(
                "1. | Proposed Position | Proposed Position | Senior Consultant"
            )
            is None
        )

    def test_ignores_name_of_staff_row(self) -> None:
        assert _extract_section_from_pipe_row("3. | Name of Staff | Name of Staff | Jane Doe") is None

    def test_no_pipe_returns_none(self) -> None:
        assert _extract_section_from_pipe_row("Employment Record") is None


class TestSplitTextIntoSectionsWBFormat:
    """Tests using synthetic World Bank-style CV text (no real PII)."""

    WB_CV_TEXT = (
        "CONSULTANT NAME\n"
        "1. | Proposed Position | Proposed Position | Senior Consultant\n"
        "2. | Name of Firm | Name of Firm | Consulting Firm\n"
        "10. | Languages | Languages | English (Fluent)\n"
        "11. | Publications | Publications | Listed below\n"
        "12. | Employment Record | Employment Record | Detailed below\n"
        "\n"
        "Led health financing analysis across multiple projects in the region.\n"
        "13. | Education | Education | Detailed below\n"
        "\n"
        "B.A. Economics, State University, 2020\n"
    )

    def test_produces_multiple_sections(self) -> None:
        sections = _split_text_into_sections(self.WB_CV_TEXT)
        headings = [s.heading for s in sections]
        assert "Employment Record" in headings
        assert "Education" in headings

    def test_header_block_is_not_a_content_section(self) -> None:
        sections = _split_text_into_sections(self.WB_CV_TEXT)
        # First section is the personal-data header block; must not be
        # confused with a structural content section.
        assert sections[0].heading not in {"Employment Record", "Education", "Publications"}

    def test_employment_section_body_has_description(self) -> None:
        sections = _split_text_into_sections(self.WB_CV_TEXT)
        emp = next(s for s in sections if s.heading == "Employment Record")
        assert "health financing" in emp.text

    def test_education_section_body_has_degree(self) -> None:
        sections = _split_text_into_sections(self.WB_CV_TEXT)
        edu = next(s for s in sections if s.heading == "Education")
        assert "State University" in edu.text

    def test_pipe_label_rows_excluded_from_body(self) -> None:
        sections = _split_text_into_sections(self.WB_CV_TEXT)
        emp = next(s for s in sections if s.heading == "Employment Record")
        # The "| Employment Record | ... | Detailed below" row must not leak into body
        assert "Detailed below" not in emp.text

    def test_metadata_pipe_rows_stripped_from_wb_section_body(self) -> None:
        # Rows 7-10 (Membership, Languages etc.) appear between Education and
        # Employment Record in the serialised text but must NOT land in Education body.
        sections = _split_text_into_sections(self.WB_CV_TEXT)
        edu = next(s for s in sections if s.heading == "Education")
        assert "Languages" not in edu.text

    def test_header_pipe_rows_kept_before_first_wb_section(self) -> None:
        # Personal info rows (before any WB section marker) must stay in the header block.
        sections = _split_text_into_sections(self.WB_CV_TEXT)
        header = sections[0]
        assert "Proposed Position" in header.text
