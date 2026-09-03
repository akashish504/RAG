"""Document-batched deck enrichment: multi-slide reply parsing, visual-only
enrichment, parser-text preservation, batching, and summary assembly. The
renderer and Sonnet enricher are faked — no LibreOffice / python-pptx / Anthropic.
"""

from __future__ import annotations

from pipeline.preprocessing.slides.backends import parse_deck_reply
from pipeline.preprocessing.slides.enricher import BatchedDeckEnricher, _merge_enrichment
from pipeline.preprocessing.slides.extractor import RenderedSlide, SlideContent
from pipeline.preprocessing.slides.models import DeckExtraction, SlideExtraction


def test_parser_title_is_authoritative() -> None:
    # Parser title wins; the model's title is ignored when the parser had one.
    s = SlideExtraction(slide_number=1, title="Intro", text="x")
    _merge_enrichment(s, SlideContent(title="Model Title", text="corrected", tables=[], visuals=""))
    assert s.title == "Intro"          # parser title preserved
    assert s.text == "corrected"       # body corrected


def test_model_title_used_only_as_fallback() -> None:
    s = SlideExtraction(slide_number=1, title="", text="")
    _merge_enrichment(s, SlideContent(title="From Image", text="body", tables=[], visuals=""))
    assert s.title == "From Image"     # parser had none → fallback to model


def test_parser_native_table_kept_over_model_transcription() -> None:
    # python-pptx read the table exactly; Haiku's vision version must NOT override it.
    parser_table = "| A | B |\n|---|---|\n| 1 | 2 |"
    s = SlideExtraction(slide_number=1, title="T", text="x", tables=[parser_table])
    _merge_enrichment(s, SlideContent(title=None, text="x",
                                      tables=["| A | B |\n|---|---|\n| ? | ? |"], visuals=""))
    assert s.tables == [parser_table]   # parser table preserved


def test_model_table_used_when_parser_found_none() -> None:
    # Image-only table (parser couldn't read it) → take the model's transcription.
    s = SlideExtraction(slide_number=1, title="T", text="x", tables=[])
    img_table = "| X | Y |\n|---|---|\n| 5 | 6 |"
    _merge_enrichment(s, SlideContent(title=None, text="x", tables=[img_table], visuals=""))
    assert s.tables == [img_table]


def test_every_slide_appears_in_output_even_when_empty() -> None:
    deck = DeckExtraction(
        document_id="d", source_s3_key="k", summary="S",
        slides=[
            SlideExtraction(slide_number=1, title="A", text="body"),
            SlideExtraction(slide_number=2, title="", text=""),  # empty slide
            SlideExtraction(slide_number=3, title="C", text="more"),
        ],
    )
    md = deck.to_markdown()
    assert "## Slide 1: A" in md
    assert "## Slide 2:" in md  # empty slide still emits a header (no slide dropped)
    assert "## Slide 3: C" in md
    assert "[Page 2]" in md


# ---------------------------------------------------------------------------
# Multi-slide reply parser
# ---------------------------------------------------------------------------

def test_parse_deck_reply_splits_by_slide() -> None:
    reply = (
        "=== SLIDE 3 ===\n"
        "VISUAL:\nCHART: bar; Data: x 10, y 20\n"
        "=== SLIDE 5 ===\n"
        "VISUAL:\nDIAGRAM: A -> B -> C\n"
    )
    out = parse_deck_reply(reply)
    assert set(out) == {3, 5}
    assert "CHART" in out[3].visuals
    assert "A -> B -> C" in out[5].visuals


def test_parse_deck_reply_empty() -> None:
    assert parse_deck_reply("") == {}
    assert parse_deck_reply("no markers here") == {}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _slide(n: int, *, visual: bool, title: str, text: str) -> RenderedSlide:
    return RenderedSlide(
        slide_number=n,
        image_png=b"png",
        classification="DIAGRAM" if visual else "TEXT",
        has_visual=visual,
        parsed_title=title,
        parsed_text=text,
        parsed_tables=[],
    )


class _FakeRenderer:
    def __init__(self, slides: list[RenderedSlide]) -> None:
        self._slides = slides

    def render(self, pptx_bytes: bytes) -> list[RenderedSlide]:
        return self._slides


class _FakeEnricher:
    name = "test"

    def __init__(self) -> None:
        self.batches: list[list[int]] = []
        self.contexts: list[str] = []
        self.summary_digest: str | None = None
        self.summary_images: list[bytes] | None = None

    def enrich_batch(self, slides, *, running_context=""):  # noqa: ANN001
        self.batches.append([s.slide_number for s in slides])
        self.contexts.append(running_context)
        # Return a visual block only for the visual slides in this batch.
        return {
            s.slide_number: SlideContent(title=None, text="", visuals=f"CHART for {s.slide_number}")
            for s in slides
            if s.has_visual
        }

    def summarize(self, digest, images=None):  # noqa: ANN001
        self.summary_digest = digest
        self.summary_images = images
        return "A rural energy project in Kenya."


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def test_enriches_only_visual_slides_and_keeps_parser_text() -> None:
    slides = [
        _slide(1, visual=False, title="Intro", text="plain bullets"),
        _slide(2, visual=True, title="Chart", text="some caption"),
    ]
    enr = _FakeEnricher()
    deck = BatchedDeckEnricher(
        renderer=_FakeRenderer(slides), enricher=enr, batch_size=20
    ).extract_deck(b"pptx", document_id="d", source_s3_key="raw/dq/d/a.pptx", title="Deck")

    s1, s2 = deck.slides
    # Text slide: untouched parser output.
    assert s1.extractor == "pptx" and s1.visuals == "" and s1.text == "plain bullets"
    # Visual slide: parser text kept, vision model visual merged in.
    assert s2.extractor == "test" and s2.escalated is True
    assert s2.text == "some caption" and "CHART for 2" in s2.visuals
    assert deck.summary == "A rural energy project in Kenya."


def test_text_only_deck_is_still_corrected() -> None:
    # Every slide is now corrected/restructured — text-only decks get the call too.
    slides = [_slide(1, visual=False, title="A", text="x"), _slide(2, visual=False, title="B", text="y")]
    enr = _FakeEnricher()
    BatchedDeckEnricher(
        renderer=_FakeRenderer(slides), enricher=enr, batch_size=20
    ).extract_deck(b"p", document_id="d", source_s3_key="k")
    assert enr.batches == [[1, 2]]  # called even with no images


def test_deck_within_batch_size_is_one_call() -> None:
    slides = [_slide(i, visual=True, title=f"T{i}", text=f"b{i}") for i in range(1, 6)]
    enr = _FakeEnricher()
    BatchedDeckEnricher(
        renderer=_FakeRenderer(slides),
        enricher=enr,
        batch_size=20,
    ).extract_deck(b"p", document_id="d", source_s3_key="k")
    assert enr.batches == [[1, 2, 3, 4, 5]]
    assert enr.contexts == [""]


def test_batches_respect_size_and_carry_context() -> None:
    slides = [_slide(i, visual=True, title=f"T{i}", text=f"b{i}") for i in range(1, 6)]
    enr = _FakeEnricher()
    BatchedDeckEnricher(
        renderer=_FakeRenderer(slides),
        enricher=enr,
        batch_size=2,
        single_call_enrichment=False,
    ).extract_deck(b"p", document_id="d", source_s3_key="k")
    assert enr.batches == [[1, 2], [3, 4], [5]]
    assert enr.contexts[0] == ""           # first batch has no prior context
    assert "T1" in enr.contexts[1] and "T2" in enr.contexts[1]  # carried forward


def test_single_call_auto_batches_when_too_many_images() -> None:
    # 26 visual slides exceeds the single-call image cap (25) → must batch.
    slides = [_slide(i, visual=True, title=f"T{i}", text=f"b{i}") for i in range(1, 27)]
    enr = _FakeEnricher()
    BatchedDeckEnricher(
        renderer=_FakeRenderer(slides),
        enricher=enr,
        batch_size=20,
        single_call_enrichment=True,
    ).extract_deck(b"p", document_id="d", source_s3_key="k")
    assert len(enr.batches) == 2  # not one giant call
    assert enr.batches[0] == list(range(1, 21))
    assert enr.batches[1] == list(range(21, 27))


def test_summary_uses_both_slide_text_and_images() -> None:
    slides = [
        _slide(1, visual=False, title="Title slide", text="Kenya off-grid energy"),
        _slide(2, visual=True, title="Impact", text="200k households"),
    ]
    enr = _FakeEnricher()
    deck = BatchedDeckEnricher(
        renderer=_FakeRenderer(slides), enricher=enr, batch_size=20
    ).extract_deck(b"p", document_id="d", source_s3_key="k")
    assert deck.summary == "A rural energy project in Kenya."
    # text digest carried both slides …
    assert "Kenya off-grid energy" in enr.summary_digest and "200k households" in enr.summary_digest
    # … and the deck images were sent too (multimodal summary).
    assert enr.summary_images and all(img == b"png" for img in enr.summary_images)


def test_no_enricher_is_parser_only() -> None:
    slides = [_slide(1, visual=True, title="Chart", text="caption")]
    deck = BatchedDeckEnricher(
        renderer=_FakeRenderer(slides), enricher=None
    ).extract_deck(b"p", document_id="d", source_s3_key="k")
    assert deck.summary is None
    assert deck.slides[0].extractor == "pptx" and deck.slides[0].visuals == ""
