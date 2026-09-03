"""Concrete slide-extraction backends: local Qwen3-VL via Ollama, and Claude Haiku.

Both take a rendered slide image and return :class:`SlideContent`. They share a
single-slide extraction prompt and a parser that splits the model's reply into
title / text / tables / visuals blocks.
"""

from __future__ import annotations

import base64
import logging
import os
import re

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from pipeline.preprocessing.slides.extractor import (
    RenderedSlide,
    SlideContent,
    attaches_image,
)

log = logging.getLogger(__name__)

# Transient Anthropic failures worth retrying so high --workers concurrency does
# not lose enrichment/summaries to a 429 or a brief 5xx.
_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
)
_claude_retry = retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=60),
    retry=retry_if_exception_type(_RETRYABLE),
)

_SLIDE_PROMPT = """\
Transcribe EVERYTHING on this single presentation slide as plain text. Miss nothing —
this is a lossless extraction, not a summary. If any element is unreadable, write
"[unclear]" rather than omitting it.

Output these labelled sections (omit a section only if it is truly absent):
TITLE: [slide title / header, or blank]
TEXT:
[every bullet, sub-bullet, paragraph, caption, label, callout, annotation, footnote,
 page/section header and source/citation line — verbatim. Nested bullets as "  -".
 Preserve units, dates, %/$ signs and footnote markers.]
TABLE:
| col | col |
|---|---|
| ... every row, every cell, including headers and totals ... |
VISUAL:
[Charts: state the chart type, the title, axis labels WITH units, the legend, and
 EVERY readable data point per series (read approximate values off the gridlines and
 mark them with ~). End with a line starting "INSIGHTS:" stating the trend/comparison
 the chart shows.
 Diagrams / frameworks / process flows: every box/node label verbatim, the grouping
 or hierarchy, and every connection/arrow with its direction and any edge label.
 Maps: every labelled region → its value/category, plus the legend.
 Pictures/screenshots/infographics: transcribe ALL text baked into the image and
 describe any data it conveys.]

Rules: reproduce text exactly; never paraphrase or summarise the content itself; never
invent numbers or labels that are not shown.
"""

_TITLE_RE = re.compile(r"^TITLE:\s*(.*)$", re.IGNORECASE | re.MULTILINE)


def parse_slide_reply(reply: str) -> SlideContent:
    """Split a model reply into title / text / tables / visuals."""
    title = None
    m = _TITLE_RE.search(reply or "")
    if m:
        title = m.group(1).strip() or None

    # Section bodies by label.
    def _section(label: str) -> str:
        pat = re.compile(
            rf"^{label}:\s*\n?(.*?)(?=^(?:TITLE|TEXT|TABLE|VISUAL):|\Z)",
            re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        mm = pat.search(reply or "")
        return mm.group(1).strip() if mm else ""

    text = _section("TEXT")
    visuals = _section("VISUAL")
    table_body = _section("TABLE")
    tables = [table_body] if table_body.strip() else []
    return SlideContent(title=title, text=text, tables=tables, visuals=visuals)


_SLIDE_BLOCK_RE = re.compile(
    r"^===\s*SLIDE\s+(\d+)\s*===\s*$", re.IGNORECASE | re.MULTILINE
)


def parse_deck_reply(reply: str) -> dict[int, SlideContent]:
    """Split a multi-slide ``=== SLIDE N ===`` reply into {slide_number: SlideContent}."""
    out: dict[int, SlideContent] = {}
    if not reply:
        return out
    matches = list(_SLIDE_BLOCK_RE.finditer(reply))
    for i, m in enumerate(matches):
        try:
            number = int(m.group(1))
        except ValueError:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(reply)
        out[number] = parse_slide_reply(reply[start:end])
    return out


def _b64(png: bytes) -> str:
    return base64.standard_b64encode(png).decode("ascii")


def b64_image_source(png: bytes) -> dict:
    """Inline base64 image source block (sync path)."""
    return {"type": "base64", "media_type": "image/png", "data": _b64(png)}


def file_image_source(file_id: str) -> dict:
    """Files-API image source block (batch path) — references an uploaded PNG."""
    return {"type": "file", "file_id": file_id}


def build_enrich_content(
    slides: list[RenderedSlide],
    *,
    running_context: str = "",
    image_source_for,  # Callable[[RenderedSlide], dict]
) -> list[dict] | None:
    """Build the correction message ``content`` for a deck, or None if no slides.

    Every slide's parser text is included. Slides that carry an image (the whole
    deck, when it has visual content) also get their image attached so the model
    can verify the parser text against it, fix losses/errors, and add visual
    content. Text-only decks send no images (the model just restructures the
    text). Single source of truth for the sync and batch paths;
    ``image_source_for`` yields inline base64 (sync) or a Files-API id (batch).
    """
    if not slides:
        return None
    with_image = [s for s in slides if attaches_image(s)]

    lines: list[str] = [_ENRICH_PROMPT]
    if running_context.strip():
        lines.append(f"\nContext from earlier slides: {running_context.strip()}\n")
    for s in slides:
        img_tag = " [IMAGE ATTACHED]" if attaches_image(s) else ""
        text_tag = " [has text]" if s.parsed_text.strip() else " [no text]"
        block = [f"[Slide {s.slide_number}]{img_tag}{text_tag}",
                 f"TITLE: {s.parsed_title or ''}"]
        if s.parsed_text.strip():
            block.append(f"TEXT:\n{s.parsed_text.strip()}")
        for tbl in s.parsed_tables:
            if tbl.strip():
                block.append(f"TABLE:\n{tbl.strip()}")
        lines.append("\n".join(block))

    content: list[dict] = [{"type": "text", "text": "\n\n".join(lines)}]
    for s in with_image:
        content.append({"type": "text", "text": f"Image for Slide {s.slide_number}:"})
        content.append({"type": "image", "source": image_source_for(s)})
    return content


def build_summary_content(digest: str, image_sources: list[dict]) -> list[dict]:
    """Build the deck-summary message ``content`` from a digest + image sources."""
    content: list[dict] = [{"type": "text", "text": _SUMMARY_PROMPT.format(digest=digest)}]
    for src in image_sources:
        content.append({"type": "image", "source": src})
    return content


def _text_from_message(msg) -> str:  # noqa: ANN001
    """Concatenate every text block of a Claude message.

    Robust to multi-block and non-text (e.g. thinking) responses — taking only
    ``content[0].text`` silently drops content or raises on a non-text first block.
    """
    blocks = getattr(msg, "content", None) or []
    return "".join(
        getattr(b, "text", "") or "" for b in blocks if getattr(b, "type", None) == "text"
    ).strip()


_ENRICH_PROMPT = """\
You are producing the clean, complete, well-structured text for each slide of a
presentation. For every slide you are given the text the parser extracted directly
from the file. Slides marked [IMAGE ATTACHED] also have their rendered image (the
ground truth) following this message, labelled by slide number.

Your job per slide: compare the parser text against the image (when present) and
output the CORRECTED, COMPLETE content for that slide.
- Keep EVERY piece of real content from the parser text. Never drop content.
- This is transcription, NOT summarization: preserve the parser's exact wording,
  numbers, names and phrasing where they are correct. Reformat and reorder for
  structure, but do NOT paraphrase, shorten, or omit anything.
- Use the image to fix what the parser got wrong: garbled/merged text, wrong reading
  order, missing bullets, mislabeled or dropped table cells, content trapped in shapes
  the parser couldn't read.
- Add what only the image shows: charts (type, axis labels with units, legend, every
  readable data point — read approximate values off the gridlines, mark with ~, then a
  line starting "INSIGHTS:"); diagrams/frameworks (every box label + how they connect,
  with arrow direction); maps (each region → value, + legend); any text baked into a
  picture; annotations, callouts, footnotes, source lines.
- Structure it cleanly: a clear title, bullets as "- " (nested as "  -"), tables as
  GitHub-markdown. Keep units, dates, %/$ and footnote markers exact.
- Never invent numbers, labels or facts not present in the text OR the image.

Slides are tagged [has text]/[no text] and [IMAGE ATTACHED] where applicable. For
text-only slides (no image), just clean up and structure the parser text — fix ordering
and formatting, but do not invent anything.

CRITICAL: output EVERY slide listed above, using its EXACT given slide number. Never
merge, split, reorder, renumber, or skip a slide — include even an image-only or empty
slide (emit its header with whatever content exists, or empty sections). The slide
number and structure must match the input exactly.

Output in EXACTLY this format and nothing else:
=== SLIDE <number> ===
TITLE: <only if the parser gave none; otherwise leave blank>
TEXT:
<all bullets/paragraphs/captions, structured>
TABLE:
<markdown table(s), if any>
VISUAL:
<chart/diagram/map content from the image, if any>
"""

_SUMMARY_PROMPT = """\
Write ONE rich, dense, self-contained summary of this consulting/project presentation.
It is embedded for semantic search AND used as the parent context for slide-level
chunks, so it must be as descriptive and information-complete as the source allows —
capture the full substance of the deck, not a thin abstract.

You are given (a) the text extracted from the slides and (b) images of representative
slides — use BOTH; the images may show charts, the title slide, or framework diagrams
the text alone misses.

Begin with ONE sentence that names exactly what this project/deck is (this first
sentence is used on its own as a slide-chunk prefix, so it must stand completely alone
and be specific). Then write a thorough multi-sentence summary (aim for 8-12 sentences,
more if the deck is rich) that captures, wherever present:
- the client / organisation and any partners, funders or stakeholders;
- the sector(s) and sub-themes;
- the geography (regions, countries, markets);
- the time period and key dates;
- the mandate / objective and the questions the work answered;
- the approach / methodology and scope of work;
- the key findings and the evidence/metrics behind them (name concrete numbers,
  percentages, figures and named programmes/instruments);
- the recommendations, decisions and the outcomes or impact.
Name concrete entities throughout so the summary matches questions like "have we worked
on <topic> in <country> for <client>?" and "what did we recommend on <theme>?".

Be comprehensive but faithful: use ONLY the content provided and never invent facts,
numbers or entities. Output flowing prose only — no headings, no bullet points, no
preamble, no meta-commentary.

Slide text:
{digest}
"""


def _model_tier_name(model: str) -> str:
    lower = model.lower()
    if "haiku" in lower:
        return "haiku"
    if "sonnet" in lower:
        return "sonnet"
    if "opus" in lower:
        return "opus"
    return "claude"


class ClaudeDeckEnricher:
    """One Claude vision call per ``enrich_batch`` invocation.

    Sends the parser's verbatim text for every slide in the batch (full deck when
    ``single_call_enrichment`` is enabled) plus an image ONLY for slides flagged
    visual, and asks for just the visual delta — keeping native text lossless.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 16384,  # headroom so a many-slide enrichment can't truncate
        summary_model: str = "claude-haiku-4-5-20251001",
    ) -> None:
        import anthropic  # noqa: PLC0415

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        # The deck summary always runs on Haiku (cheap, one call/doc) regardless of
        # the enrichment model.
        self._summary_model = summary_model
        self.name = _model_tier_name(model)

    @_claude_retry
    def enrich_batch(
        self, slides: list[RenderedSlide], *, running_context: str = ""
    ) -> dict[int, SlideContent]:
        """Return {slide_number: SlideContent(visuals=…)} for the image slides only."""
        content = build_enrich_content(
            slides,
            running_context=running_context,
            image_source_for=lambda s: b64_image_source(s.image_png),
        )
        if content is None:
            return {}

        with self._client.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": content}],
        ) as stream:
            msg = stream.get_final_message()
        return parse_deck_reply(_text_from_message(msg))

    @_claude_retry
    def summarize(self, digest: str, images: list[bytes] | None = None) -> str:
        """One Haiku call → deck-level summary from the slide text AND deck images."""
        sources = [b64_image_source(png) for png in (images or []) if png]
        content = build_summary_content(digest, sources)
        with self._client.messages.stream(
            model=self._summary_model,
            max_tokens=2048,  # room for a dense, descriptive summary
            messages=[{"role": "user", "content": content}],
        ) as stream:
            msg = stream.get_final_message()
        return _text_from_message(msg)


# Backward-compatible alias (D-Quals previously wired to Sonnet).
SonnetBatchEnricher = ClaudeDeckEnricher


class PptxParserExtractor:
    """Free cheap tier — native python-pptx content, no GPU and no LLM.

    Reads the geometry-ordered title / text / tables the renderer already parsed
    off the slide XML. Visual content (charts, diagrams baked into pictures) is
    left empty so the escalation gate routes those slides to the multimodal
    Haiku tier, which reads the rendered image.
    """

    name = "pptx"

    def extract(self, slide: RenderedSlide) -> SlideContent:
        return SlideContent(
            title=slide.parsed_title,
            text=slide.parsed_text,
            tables=list(slide.parsed_tables),
            visuals="",
        )


class OllamaVLMExtractor:
    """Local Qwen3-VL served by Ollama (free on a GPU instance)."""

    name = "local_vlm"

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout_s: int = 120,
    ) -> None:
        self._model = model or os.environ.get("OLLAMA_VLM_MODEL", "qwen3-vl:3b")
        self._base = (base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
        self._timeout = timeout_s

    def extract(self, slide: RenderedSlide) -> SlideContent:
        import requests  # noqa: PLC0415

        resp = requests.post(
            f"{self._base}/api/generate",
            json={
                "model": self._model,
                "prompt": _SLIDE_PROMPT,
                "images": [_b64(slide.image_png)],
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return parse_slide_reply(resp.json().get("response", ""))


class HaikuVLMExtractor:
    """Claude Haiku vision — escalation backend for hard visual slides."""

    name = "haiku"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 4096,
    ) -> None:
        import anthropic  # noqa: PLC0415

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    @_claude_retry
    def extract(self, slide: RenderedSlide) -> SlideContent:
        with self._client.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png",
                        "data": _b64(slide.image_png)}},
                    {"type": "text", "text": _SLIDE_PROMPT},
                ],
            }],
        ) as stream:
            msg = stream.get_final_message()
        return parse_slide_reply(msg.content[0].text if msg.content else "")
