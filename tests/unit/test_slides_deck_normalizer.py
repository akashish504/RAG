"""SlidesDeckNormalizer routing: PPTX → tiered extractor, everything else
(PDF, DOCX/DOC, images) → llm_content, plus the empty-extraction fallback.
Collaborators are faked so no LibreOffice / Anthropic / GPU is needed.
"""

from __future__ import annotations

from pipeline.preprocessing.normalizers.slides_deck import SlidesDeckNormalizer


class _FakeDeck:
    slide_count = 2
    escalated_count = 1
    summary = "A deck about X."

    def __init__(self, markdown: str = "## Slide 1: X\n\n[Page 1]") -> None:
        self._md = markdown

    def to_markdown(self) -> str:
        return self._md


class _FakeTiered:
    def __init__(self, markdown: str = "## Slide 1: X\n\n[Page 1]") -> None:
        self.calls = 0
        self._md = markdown

    def extract_deck(self, binary, *, document_id, source_s3_key):  # noqa: ANN001
        self.calls += 1
        return _FakeDeck(self._md)


class _FakePdf:
    def __init__(self) -> None:
        self.calls = 0

    def normalize(self, binary, filename):  # noqa: ANN001
        self.calls += 1
        return "PDF-TEXT"


def _wire(tiered: _FakeTiered | None = None, pdf: _FakePdf | None = None) -> SlidesDeckNormalizer:
    n = SlidesDeckNormalizer(api_key="test")
    n._extractor = tiered or _FakeTiered()
    n._pdf_fallback = pdf or _FakePdf()
    return n


def test_pptx_routes_to_tiered_extractor() -> None:
    n = _wire()
    out = n.normalize(b"deck", "proposal.pptx")
    assert out is not None and "Slide 1" in out
    assert n._extractor.calls == 1
    assert n._pdf_fallback.calls == 0  # no Claude whole-deck call


def test_pdf_routes_to_llm_content() -> None:
    n = _wire()
    assert n.normalize(b"file", "deliverable.pdf") == "PDF-TEXT"
    assert n._pdf_fallback.calls == 1
    assert n._extractor.calls == 0


def test_docx_routes_to_llm_content() -> None:
    # DOCX must not silently fall through to a raw upload — llm_content handles it.
    n = _wire()
    assert n.normalize(b"file", "deliverable.docx") == "PDF-TEXT"
    assert n._pdf_fallback.calls == 1
    assert n._extractor.calls == 0


def test_image_routes_to_llm_content() -> None:
    # Images are delegated too (llm_content does the image extraction); only
    # PPTX/PPT use the slide renderer.
    n = _wire()
    assert n.normalize(b"img", "logo.png") == "PDF-TEXT"
    assert n._pdf_fallback.calls == 1
    assert n._extractor.calls == 0


def test_pptx_empty_extraction_falls_back_to_llm_content() -> None:
    n = _wire(tiered=_FakeTiered(markdown="   "))  # extractor produced nothing
    assert n.normalize(b"deck", "weird.pptx") == "PDF-TEXT"
    assert n._extractor.calls == 1
    assert n._pdf_fallback.calls == 1  # fell through to whole-deck Claude
