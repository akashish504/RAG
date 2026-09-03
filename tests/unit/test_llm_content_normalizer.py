"""Unit tests for LLMContentNormalizer.

All Claude API calls are mocked — no live API keys required.
LibreOffice is mocked as absent so PPTX tests always use the text fallback path.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from pipeline.preprocessing.normalizers.llm_content import (
    ContentNormalizerSettings,
    LLMContentNormalizer,
    get_content_normalizer_settings,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_API_KEY = "sk-ant-test-fake"


def _make_normalizer(**settings_kwargs) -> LLMContentNormalizer:
    defaults = dict(
        model="claude-sonnet-4-6",
        max_output_tokens=32768,
        page_batch_size=10,
        single_call_max_pages=10,
    )
    defaults.update(settings_kwargs)
    settings = ContentNormalizerSettings(**defaults)
    with patch("pipeline.preprocessing.normalizers.llm_content.anthropic.Anthropic"):
        n = LLMContentNormalizer(api_key=_FAKE_API_KEY, settings=settings)
    n._client = MagicMock()
    return n


def _claude_returns(normalizer: LLMContentNormalizer, text: str) -> None:
    """Configure the mocked Claude client to return `text` via the streaming API.

    ``_call_claude`` uses ``with client.messages.stream(...) as s: s.get_final_message()``,
    so we wire a context manager whose get_final_message() returns the message.
    """
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    msg.stop_reason = "end_turn"
    cm = MagicMock()
    cm.__enter__.return_value.get_final_message.return_value = msg
    cm.__exit__.return_value = False
    normalizer._client.messages.stream.return_value = cm


def _patch_pdf_pages(n: int):
    return patch(
        "pipeline.preprocessing.normalizers.llm_content._pdf_page_count",
        return_value=n,
    )


# ---------------------------------------------------------------------------
# File-type dispatch
# ---------------------------------------------------------------------------


def test_pdf_dispatches_to_document_block(tmp_path):
    n = _make_normalizer()
    _claude_returns(n, "## Section 1\nContent here")
    with _patch_pdf_pages(5):
        result = n.normalize(b"%PDF-fake", "report.pdf")
    assert result is not None
    call_args = n._client.messages.stream.call_args
    content = call_args.kwargs["messages"][0]["content"]
    doc_block = next((b for b in content if isinstance(b, dict) and b.get("type") == "document"), None)
    assert doc_block is not None, "Expected a document content block for PDF"
    assert doc_block["source"]["media_type"] == "application/pdf"


def test_image_png_dispatches_to_image_block():
    n = _make_normalizer()
    _claude_returns(n, "This image shows a bar chart...")
    result = n.normalize(b"\x89PNG", "chart.png")
    assert result is not None
    call_args = n._client.messages.stream.call_args
    content = call_args.kwargs["messages"][0]["content"]
    img_block = next((b for b in content if isinstance(b, dict) and b.get("type") == "image"), None)
    assert img_block is not None, "Expected an image content block for PNG"
    assert img_block["source"]["media_type"] == "image/png"


def test_image_jpeg_dispatches_to_image_block():
    n = _make_normalizer()
    _claude_returns(n, "A photo of...")
    n.normalize(b"\xff\xd8", "photo.jpg")
    call_args = n._client.messages.stream.call_args
    content = call_args.kwargs["messages"][0]["content"]
    img_block = next(b for b in content if isinstance(b, dict) and b.get("type") == "image")
    assert img_block["source"]["media_type"] == "image/jpeg"


def test_unsupported_extension_returns_none():
    n = _make_normalizer()
    result = n.normalize(b"some bytes", "document.xlsx")
    assert result is None
    n._client.messages.stream.assert_not_called()


def test_docx_returns_none_without_claude_call():
    n = _make_normalizer()
    result = n.normalize(b"PK\x03\x04", "resume.docx")
    assert result is None
    n._client.messages.stream.assert_not_called()


# ---------------------------------------------------------------------------
# PPTX — text fallback (no LibreOffice)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("pptx"),
    reason="python-pptx not installed",
)
def test_pptx_fallback_uses_slide_heading_format():
    """When LibreOffice is absent, fallback output uses ## Slide N: Title format."""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    layout = prs.slide_layouts[6]
    slide = prs.slides.add_slide(layout)
    txBox = slide.shapes.add_textbox(Inches(0), Inches(0), Inches(8), Inches(1))
    txBox.text_frame.text = "My Title"
    txBox2 = slide.shapes.add_textbox(Inches(0), Inches(1), Inches(8), Inches(4))
    txBox2.text_frame.text = "Bullet content"
    buf = io.BytesIO()
    prs.save(buf)
    pptx_bytes = buf.getvalue()

    n = _make_normalizer()
    with patch(
        "pipeline.preprocessing.normalizers.pptx_converter.pptx_to_pdf_bytes",
        return_value=None,  # LibreOffice absent
    ):
        result = n.normalize(pptx_bytes, "deck.pptx")

    assert result is not None
    assert "## Slide 1:" in result


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("pptx"),
    reason="python-pptx not installed",
)
def test_pptx_fallback_no_claude_call_when_libreoffice_absent():
    """Fallback path formats slides directly without a Claude call."""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    layout = prs.slide_layouts[6]
    slide = prs.slides.add_slide(layout)
    txBox = slide.shapes.add_textbox(Inches(0), Inches(0), Inches(8), Inches(1))
    txBox.text_frame.text = "Title Only"
    buf = io.BytesIO()
    prs.save(buf)

    n = _make_normalizer()
    with patch(
        "pipeline.preprocessing.normalizers.pptx_converter.pptx_to_pdf_bytes",
        return_value=None,
    ):
        n.normalize(buf.getvalue(), "deck.pptx")

    # No Claude call in fallback path
    n._client.messages.stream.assert_not_called()


# ---------------------------------------------------------------------------
# PPTX — LibreOffice path
# ---------------------------------------------------------------------------


def test_pptx_uses_libreoffice_path_when_available():
    """When LibreOffice returns PDF bytes, they are sent to Claude as a document block."""
    n = _make_normalizer()
    _claude_returns(n, "## Slide 1: Intro\nContent")
    fake_pdf = b"%PDF-converted"

    with patch(
        "pipeline.preprocessing.normalizers.pptx_converter.pptx_to_pdf_bytes",
        return_value=fake_pdf,
    ), _patch_pdf_pages(3):
        result = n.normalize(b"pptx-bytes", "deck.pptx")

    assert result is not None
    call_args = n._client.messages.stream.call_args
    content = call_args.kwargs["messages"][0]["content"]
    doc_block = next(b for b in content if isinstance(b, dict) and b.get("type") == "document")
    assert doc_block["source"]["media_type"] == "application/pdf"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_exception_returns_none_not_raised():
    n = _make_normalizer()
    n._client.messages.stream.side_effect = RuntimeError("API down")
    with _patch_pdf_pages(5):
        result = n.normalize(b"%PDF-fake", "report.pdf")
    assert result is None


def test_empty_bytes_returns_none_or_str():
    n = _make_normalizer()
    _claude_returns(n, "")
    with _patch_pdf_pages(0):
        result = n.normalize(b"", "empty.pdf")
    assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_llm_content_registered():
    from pipeline.preprocessing.normalizers.registry import registered_names
    assert "llm_content" in registered_names()


def test_default_model_is_haiku(monkeypatch):
    monkeypatch.delenv("CONTENT_NORMALIZER_MODEL", raising=False)
    monkeypatch.delenv("CONTENT_NORMALIZER_MAX_OUTPUT_TOKENS", raising=False)
    settings = get_content_normalizer_settings()
    assert settings.model == "claude-haiku-4-5-20251001"
    assert settings.max_output_tokens == 32768


def test_pdf_single_call_when_under_10_pages():
    n = _make_normalizer(single_call_max_pages=10)
    _claude_returns(n, "## Intro\nBody")
    with _patch_pdf_pages(9):
        n.normalize(b"%PDF-fake", "short.pdf")
    assert n._client.messages.stream.call_count == 1
    prompt = n._client.messages.stream.call_args.kwargs["messages"][0]["content"][-1]["text"]
    assert "entire PDF document" in prompt
    assert "INSIGHTS:" in prompt


def test_pdf_batched_when_10_or_more_pages():
    n = _make_normalizer(page_batch_size=10, single_call_max_pages=10)
    _claude_returns(n, "## Section\nCONTENT")
    with _patch_pdf_pages(15), patch(
        "pipeline.preprocessing.normalizers.llm_content._split_pdf_into_batches",
        return_value=[(b"batch1", 1, 10), (b"batch2", 11, 15)],
    ):
        n.normalize(b"%PDF-fake", "long.pdf")
    assert n._client.messages.stream.call_count == 2
    second_prompt = n._client.messages.stream.call_args_list[1].kwargs["messages"][0]["content"][-1]["text"]
    assert "CONTEXT" in second_prompt


def test_claude_call_uses_configured_model_and_max_tokens():
    n = _make_normalizer(model="claude-sonnet-4-6", max_output_tokens=32000)
    _claude_returns(n, "ok")
    with _patch_pdf_pages(3):
        n.normalize(b"%PDF-fake", "doc.pdf")
    kwargs = n._client.messages.stream.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["max_tokens"] == 32000
