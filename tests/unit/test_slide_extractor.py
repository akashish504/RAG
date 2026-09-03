"""Tests for tiered slide extraction: classification, escalation gate, orchestration,
and the slides.json round-trip. Backends/renderer are faked — no GPU/Ollama/LibreOffice.
"""

from __future__ import annotations

from pipeline.preprocessing.slides.backends import (
    PptxParserExtractor,
    _text_from_message,
    parse_slide_reply,
)
from pipeline.preprocessing.slides.extractor import (
    RenderedSlide,
    SlideContent,
    TieredSlideExtractor,
    attaches_image,
    needs_escalation,
)
from pipeline.preprocessing.slides.models import DeckExtraction
from pipeline.preprocessing.slides.render import (
    _table_markdown,
    _text_frame_markdown,
    classify_from_stats,
    is_visual_from_stats,
    needs_image,
)


# ---------------------------------------------------------------------------
# Classification (pure)
# ---------------------------------------------------------------------------

def test_classify_text_slide() -> None:
    assert classify_from_stats({"text_chars": 400, "n_shapes": 3}) == "TEXT"


def test_classify_chart_is_visual() -> None:
    assert classify_from_stats({"has_chart": True, "text_chars": 30, "n_shapes": 2}) == "DIAGRAM"
    assert classify_from_stats({"has_chart": True, "text_chars": 500, "n_shapes": 5}) == "MIXED"


def test_classify_smartart_group_is_framework() -> None:
    assert classify_from_stats({"has_group": True, "text_chars": 60, "n_shapes": 8}) == "FRAMEWORK"


def test_classify_picture_only_is_image() -> None:
    assert classify_from_stats({"has_picture": True, "text_chars": 10, "n_shapes": 1}) == "IMAGE"


def test_classify_table_and_empty() -> None:
    assert classify_from_stats({"has_table": True, "text_chars": 20, "n_shapes": 1}) == "TABLE"
    assert classify_from_stats({"text_chars": 0, "n_shapes": 0}) == "EMPTY"


# ---------------------------------------------------------------------------
# Escalation gate
# ---------------------------------------------------------------------------

def _slide(cls: str) -> RenderedSlide:
    return RenderedSlide(slide_number=1, image_png=b"png", classification=cls)  # type: ignore[arg-type]


def test_text_slide_never_escalates() -> None:
    rich = SlideContent(title="t", text="lots of real text " * 5)
    assert needs_escalation(_slide("TEXT"), rich) is False


def test_visual_slide_with_thin_output_escalates() -> None:
    thin = SlideContent(title=None, text="", visuals="")
    assert needs_escalation(_slide("DIAGRAM"), thin) is True


def test_framework_without_visual_block_escalates() -> None:
    # Has some text but no VISUAL block on a framework slide → escalate.
    c = SlideContent(title="Framework", text="a few words here only", visuals="")
    assert needs_escalation(_slide("FRAMEWORK"), c) is True


def test_visual_slide_with_good_output_stays_local() -> None:
    good = SlideContent(
        title="Chart",
        text="",
        visuals=(
            "CHART: Revenue by region\nType: bar\nX-axis: region\nY-axis: USD m\n"
            "Data: LATAM 10, EMEA 20, APAC 30, NA 45\nINSIGHTS: NA highest, LATAM lowest"
        ),
    )
    assert needs_escalation(_slide("DIAGRAM"), good) is False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

class _FakeRenderer:
    def __init__(self, slides: list[RenderedSlide]) -> None:
        self._slides = slides

    def render(self, pptx_bytes: bytes) -> list[RenderedSlide]:
        return self._slides


class _FakeBackend:
    def __init__(self, name: str, content: SlideContent) -> None:
        self.name = name
        self._content = content
        self.calls = 0

    def extract(self, slide: RenderedSlide) -> SlideContent:
        self.calls += 1
        return self._content


def test_orchestrator_escalates_only_weak_visual_slides() -> None:
    slides = [
        RenderedSlide(1, b"img1", "TEXT"),       # local handles
        RenderedSlide(2, b"img2", "DIAGRAM"),    # weak local → escalate
    ]
    local = _FakeBackend("local_vlm", SlideContent(title=None, text="", visuals=""))
    haiku = _FakeBackend("haiku", SlideContent(title="Chart", text="x", visuals="CHART: full data"))
    deck = TieredSlideExtractor(
        renderer=_FakeRenderer(slides), local_backend=local, haiku_backend=haiku
    ).extract_deck(b"pptx", document_id="doc1", source_s3_key="raw/kl/doc1/a.pptx")

    assert local.calls == 2          # local ran on every slide
    assert haiku.calls == 1          # only the weak visual slide escalated
    assert deck.slide_count == 2
    assert deck.escalated_count == 1
    assert deck.slides[0].extractor == "local_vlm" and deck.slides[0].escalated is False
    assert deck.slides[1].extractor == "haiku" and deck.slides[1].escalated is True


def test_orchestrator_without_haiku_keeps_local() -> None:
    slides = [RenderedSlide(1, b"img", "DIAGRAM")]
    local = _FakeBackend("local_vlm", SlideContent(title=None, text="", visuals=""))
    deck = TieredSlideExtractor(
        renderer=_FakeRenderer(slides), local_backend=local, haiku_backend=None
    ).extract_deck(b"x", document_id="d", source_s3_key="k")
    assert deck.escalated_count == 0
    assert deck.slides[0].extractor == "local_vlm"


# ---------------------------------------------------------------------------
# Reply parsing + slides.json round-trip
# ---------------------------------------------------------------------------

def test_parse_slide_reply() -> None:
    reply = (
        "TITLE: Market Entry\n"
        "TEXT:\n- point one\n- point two\n"
        "TABLE:\n| A | B |\n|---|---|\n| 1 | 2 |\n"
        "VISUAL:\nCHART: bar; Data: x 10, y 20\n"
    )
    c = parse_slide_reply(reply)
    assert c.title == "Market Entry"
    assert "point one" in c.text and "point two" in c.text
    assert c.tables and "| 1 | 2 |" in c.tables[0]
    assert "CHART" in c.visuals


# ---------------------------------------------------------------------------
# Free python-pptx parser tier (no GPU / no LLM)
# ---------------------------------------------------------------------------

def test_pptx_parser_extractor_reads_parsed_fields() -> None:
    slide = RenderedSlide(
        slide_number=1,
        image_png=b"img",
        classification="TEXT",
        parsed_title="Overview",
        parsed_text="- bullet one\n- bullet two",
        parsed_tables=["| A | B |\n|---|---|\n| 1 | 2 |"],
    )
    c = PptxParserExtractor().extract(slide)
    assert c.title == "Overview"
    assert "bullet one" in c.text and "bullet two" in c.text
    assert c.tables == ["| A | B |\n|---|---|\n| 1 | 2 |"]
    assert c.visuals == ""  # parser never invents visual content → leaves it for Haiku


class _FakeRun:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakePara:
    def __init__(self, text: str, level: int = 0) -> None:
        self.runs = [_FakeRun(text)] if text else []
        self.level = level
        self.text = text


class _FakeTextFrame:
    def __init__(self, paras: list[_FakePara]) -> None:
        self.paragraphs = paras


def test_text_frame_markdown_preserves_nesting() -> None:
    tf = _FakeTextFrame([_FakePara("Top", 0), _FakePara("Child", 1), _FakePara("", 0)])
    md = _text_frame_markdown(tf)
    assert md == "- Top\n  - Child"  # empty paragraph dropped, level → indent


class _FakeCell:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeRow:
    def __init__(self, cells: list[str]) -> None:
        self.cells = [_FakeCell(c) for c in cells]


class _FakeTable:
    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = [_FakeRow(r) for r in rows]


def test_table_markdown_renders_header_and_body() -> None:
    md = _table_markdown(_FakeTable([["Region", "USD"], ["LATAM", "10"], ["EMEA", "20"]]))
    assert md.splitlines()[0] == "| Region | USD |"
    assert md.splitlines()[1] == "|---|---|"
    assert "| LATAM | 10 |" in md and "| EMEA | 20 |" in md


# ---------------------------------------------------------------------------
# Parser-quality gate — don't blindly trust the python-pptx parser
# ---------------------------------------------------------------------------

def test_needs_image_flags_thin_parser_output() -> None:
    # Shapes report 200 chars but the parser only got 4 → likely missed text.
    assert needs_image({"text_chars": 200, "n_shapes": 3}, "tiny", []) is True


def test_needs_image_ok_when_parser_captured_enough() -> None:
    assert needs_image({"text_chars": 200, "n_shapes": 3}, "x" * 150, []) is False


def test_needs_image_false_on_empty_slide() -> None:
    assert needs_image({"text_chars": 0, "n_shapes": 0}, "", []) is False


def test_smartart_group_is_visual() -> None:
    # SmartArt is detected as a group → visual → gets an image (text isn't parseable).
    assert is_visual_from_stats({"has_group": True}) is True


def test_chart_always_visual() -> None:
    assert is_visual_from_stats({"has_chart": True, "text_chars": 5000}) is True


def test_logo_on_text_rich_slide_is_not_visual() -> None:
    # A picture (logo/decoration) on a slide full of parsed text must NOT escalate —
    # branded decks have one on every slide; we don't ship them all to the model.
    assert is_visual_from_stats({"has_picture": True, "text_chars": 800}) is False


def test_picture_dominant_slide_is_visual() -> None:
    # A picture with little text likely IS the content (screenshot/infographic).
    assert is_visual_from_stats({"has_picture": True, "text_chars": 20}) is True


def test_attaches_image_combines_visual_and_parser_gate() -> None:
    base = dict(slide_number=1, image_png=b"x", classification="TEXT")
    assert attaches_image(RenderedSlide(**base, has_visual=False, needs_image=True)) is True
    assert attaches_image(RenderedSlide(**base, has_visual=True, needs_image=False)) is True
    assert attaches_image(RenderedSlide(**base, has_visual=False, needs_image=False)) is False


# ---------------------------------------------------------------------------
# Claude response parsing ("the last call")
# ---------------------------------------------------------------------------

class _Block:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class _Msg:
    def __init__(self, blocks) -> None:  # noqa: ANN001
        self.content = blocks


def test_text_from_message_joins_text_blocks_only() -> None:
    msg = _Msg([_Block("thinking", "hmm"), _Block("text", "=== SLIDE 1 ==="),
                _Block("text", "\nVISUAL:\nx")])
    assert _text_from_message(msg) == "=== SLIDE 1 ===\nVISUAL:\nx"


def test_text_from_message_handles_empty_or_none() -> None:
    assert _text_from_message(_Msg([])) == ""
    assert _text_from_message(_Msg(None)) == ""


def test_deck_json_roundtrip_and_markdown() -> None:
    slides = [RenderedSlide(1, b"i", "TEXT")]
    local = _FakeBackend("local_vlm", SlideContent(title="Intro", text="hello", visuals=""))
    deck = TieredSlideExtractor(
        renderer=_FakeRenderer(slides), local_backend=local, haiku_backend=None
    ).extract_deck(b"x", document_id="d1", source_s3_key="raw/kl/d1/a.pptx", title="Deck")

    d = deck.to_dict()
    back = DeckExtraction.from_dict(d)
    assert back.document_id == "d1"
    assert back.slides[0].title == "Intro"
    md = deck.to_markdown()
    assert "## Slide 1: Intro" in md and "[Page 1]" in md and "DOCUMENT_TITLE: Deck" in md
