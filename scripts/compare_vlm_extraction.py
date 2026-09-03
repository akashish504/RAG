"""VLM extraction quality test on a single PPTX.

Strategy (mirrors production intent):
  - Text-only slides: python-pptx output used directly, no model call.
  - Visual slides (charts / diagrams / images / mixed): one Qwen call per slide,
    image-only, extract content from the rendered PNG.

Usage (run on the GPU EC2 with vLLM already serving the model):

    vllm serve Qwen/Qwen2.5-VL-7B-Instruct --host 0.0.0.0 --port 8000

    python scripts/compare_vlm_extraction.py \\
        --url "https://..." \\
        --model Qwen/Qwen2.5-VL-7B-Instruct \\
        --vllm-url http://localhost:8000 \\
        --out outputs/extraction_7b.md
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

log = logging.getLogger(__name__)

# Intentionally NOT using _SUMMARY_PROMPT from backends — that prompt is tuned for
# Claude (long, rich output). InternVL loops on it. Use a tight local prompt instead.
from pipeline.preprocessing.slides.extractor import (  # noqa: E402
    RenderedSlide,
    attaches_image,
)
from pipeline.preprocessing.slides.backends import SlideContent  # noqa: E402
from pipeline.preprocessing.slides.enricher import (  # noqa: E402
    _base_slide,
    _merge_enrichment,
)
from pipeline.preprocessing.slides.render import PptxSlideRenderer  # noqa: E402
from pipeline.preprocessing.slides.models import DeckExtraction  # noqa: E402


# Tight summary prompt for local VLMs with 8K context — Claude's rich prompt causes
# InternVL to loop. This one demands exactly 3-5 sentences and stops.
_SUMMARY_PROMPT = """\
Write a 3-5 sentence summary of this presentation. Cover: what the document is, \
who it is for, the main topics, and any key facts or numbers. Be specific and factual. \
Do not repeat yourself. Output only the summary, nothing else.

{digest}
"""

# ---------------------------------------------------------------------------
# Local reply parser — tolerates "### TITLE:" and bare "TITLE:" equally
# ---------------------------------------------------------------------------

_LABEL_RE = re.compile(
    r"^(?:#{1,3}\s*)?(TITLE|TEXT|TABLE|VISUAL)\s*:",
    re.IGNORECASE | re.MULTILINE,
)
# Page-number lines emitted by python-pptx (standalone digit lines like "- 5")
_PAGE_NUM_RE = re.compile(r"^\s*-?\s*\d{1,3}\s*$", re.MULTILINE)


def _parse_vlm_reply(reply: str) -> SlideContent:
    """Parse a VLM reply that may use '### TITLE:' or bare 'TITLE:' labels.

    Handles two output styles:
      (a) Labelled: TITLE: / TEXT: / TABLE: / VISUAL: sections
      (b) Free-form: model skips TEXT: label and goes straight from TITLE: into
          '### Block heading' paragraphs — treat everything after TITLE: that
          isn't TABLE:/VISUAL: as the text body.
    """
    if not reply:
        return SlideContent(title=None, text="", tables=[], visuals="")

    # Split on any recognised section header (with or without ### prefix)
    splits = list(_LABEL_RE.finditer(reply))
    sections: dict[str, str] = {}
    for i, m in enumerate(splits):
        label = m.group(1).upper()
        start = m.end()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(reply)
        sections[label] = reply[start:end].strip()

    title_raw = sections.get("TITLE", "").strip()
    # Reject placeholder echoes like "[slide title or main header]"
    title = title_raw if (title_raw and not title_raw.startswith("[")) else None

    text = sections.get("TEXT", "").strip()
    visuals = sections.get("VISUAL", "").strip()
    table_body = sections.get("TABLE", "").strip()
    tables = [table_body] if table_body else []

    # Fallback: model output TITLE: then went straight into ### block headings
    # without ever writing "TEXT:". In that case sections["TITLE"] captured the
    # entire body. Treat everything after the first newline of the title as text.
    if not text and not visuals and title and "\n" in title:
        lines = title.split("\n", 1)
        title = lines[0].strip() or None
        text = lines[1].strip()

    # Strip non-content filler phrases the model emits when a section is absent.
    _FILLER = re.compile(
        r"^\[(?:No visual (?:content|data)(?: described| provided| present)?|unclear|none)\]\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    text = _FILLER.sub("", text).strip()
    visuals = _FILLER.sub("", visuals).strip()

    # If the model produced no TITLE: line, pull the title from the first non-blank
    # text line — strip leading markdown heading hashes and bullet markers first.
    if not title and text:
        first_line = text.split("\n")[0].strip()
        clean = re.sub(r"^#{1,6}\s*", "", first_line).strip()  # "##### FOO" → "FOO"
        clean = re.sub(r"^-\s+", "", clean).strip()             # "- FOO" → "FOO"
        if clean:
            title = clean
            text = text[len(first_line):].strip()

    return SlideContent(title=title, text=text, tables=tables, visuals=visuals)

# ---------------------------------------------------------------------------
# Prompt for visual slides — image-only, no pre-extracted text
# ---------------------------------------------------------------------------

_VISUAL_SLIDE_PROMPT = """\
Transcribe EVERYTHING on this single presentation slide as plain text. Miss nothing —
this is a lossless extraction, not a summary. If any element is unreadable, write
"[unclear]" rather than omitting it.

The slide may have multiple visual blocks — colored sections, columns, boxes, or panels
arranged side by side or stacked. Finish one block entirely before starting the next.
Reading order: top to bottom; when blocks are side by side, go left to right.

Output these labelled sections (omit a section only if it is truly absent):
TITLE: <the slide's main title or top header>
TEXT:
[Every bullet, sub-bullet, paragraph, caption, label, callout, annotation, footnote.
 Each distinct block on its own line, separated by a blank line. Use the block's own
 heading as a "### " prefix if it has one. Nested bullets as "  -".
 Preserve all numbers, units, dates, % and $ exactly.
 Skip navigation tabs, step indicators, and page numbers — these are chrome.]
TABLE:
| col | col |
|---|---|
| ... every row, every cell, including headers and totals ... |
VISUAL:
[Charts: type, axis labels with units, legend, every readable data point marked with ~,
 then "INSIGHTS: <trend>".
 Diagrams/frameworks: every box/node label verbatim and every connection with direction.]

Rules: reproduce text exactly; never paraphrase or summarise; never invent content.
Output ONLY the labelled sections above — no preamble, no trailing commentary.
"""


# ---------------------------------------------------------------------------
# Qwen caller
# ---------------------------------------------------------------------------

class QwenVLM:
    """Calls vLLM once per visual slide. Text slides are skipped entirely."""

    name = "qwen_vlm"

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://localhost:8000",
        max_tokens: int = 4096,
        timeout_s: int = 300,
    ) -> None:
        self._model = model
        self._base = base_url.rstrip("/")
        self._max_tokens = max_tokens
        self._timeout = timeout_s

    def _chat(self, messages: list[dict]) -> str:
        body = json.dumps({
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": 0,
        }).encode()
        request = urllib.request.Request(
            f"{self._base}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()

    def _image_block(self, png: bytes) -> dict:
        b64 = base64.standard_b64encode(png).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}

    def extract_visual_slide(self, slide: RenderedSlide) -> SlideContent:
        """Send the full slide image to the VLM — lets it understand layout."""
        if not slide.image_png:
            return SlideContent(title=None, text="", tables=[], visuals="")
        user_content = [
            self._image_block(slide.image_png),
            {"type": "text", "text": _VISUAL_SLIDE_PROMPT},
        ]
        reply = self._chat([{"role": "user", "content": user_content}])
        return _parse_vlm_reply(reply)

    def summarize(self, digest: str, images: list[bytes] | None = None) -> str:
        user_content: list[dict] = [
            {"type": "text", "text": _SUMMARY_PROMPT.format(digest=digest)}
        ]
        # InternVL has a lower multi-image limit than Claude — cap at 4 representative slides.
        capped = (images or [])[:4]
        for png in capped:
            if png:
                user_content.append(self._image_block(png))
        # Cap summary tokens tightly — the model loops if given too much budget.
        body = json.dumps({
            "model": self._model,
            "messages": [{"role": "user", "content": user_content}],
            "max_tokens": 512,
            "temperature": 0,
        }).encode()
        request = urllib.request.Request(
            f"{self._base}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------

def download_pptx(url: str) -> bytes:
    log.info("Downloading PPTX from %s ...", url)
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = resp.read()
    log.info("Downloaded %.1f KB", len(data) / 1024)
    return data


# ---------------------------------------------------------------------------
# Extraction orchestrator
# ---------------------------------------------------------------------------

def extract_deck(
    pptx_bytes: bytes,
    vlm: QwenVLM,
    *,
    label: str,
    dpi: int = 110,
    max_slides: int | None = None,
    workers: int = 16,
) -> DeckExtraction:
    renderer = PptxSlideRenderer(dpi=dpi)

    log.info("[%s] Rendering slides at %d DPI ...", label, dpi)
    rendered = renderer.render(pptx_bytes)

    if max_slides:
        rendered = rendered[:max_slides]

    slides = [_base_slide(r) for r in rendered]
    # Strip standalone page-number lines (e.g. "- 5") from parser output — these
    # are slide-number chrome that python-pptx picks up from a text box on each slide.
    for s in slides:
        s.text = _PAGE_NUM_RE.sub("", s.text).strip()
    by_number = {s.slide_number: s for s in slides}

    n_visual = sum(1 for r in rendered if attaches_image(r))
    log.info("[%s] %d slides total, %d visual (will call Qwen), %d text-only (parser only)",
             label, len(rendered), n_visual, len(rendered) - n_visual)

    visual_slides = [r for r in rendered if attaches_image(r)]
    text_only = [r for r in rendered if not attaches_image(r)]
    for r in text_only:
        log.info("[%s] slide %d — TEXT, using parser output", label, r.slide_number)

    log.info("[%s] sending %d visual slides to VLM in parallel (workers=%d) ...",
             label, len(visual_slides), workers)

    def _call(r: RenderedSlide):
        n_regions = len(r.shape_stats.get("visual_regions") or [])
        log.info("[%s] slide %d — VISUAL (%s), %d region(s), calling VLM ...",
                 label, r.slide_number, r.classification, n_regions)
        t0 = time.time()
        content = vlm.extract_visual_slide(r)
        log.info("[%s] slide %d done in %.1fs — title=%r text_len=%d",
                 label, r.slide_number, time.time() - t0, content.title, len(content.text))
        return r.slide_number, content

    n_enriched = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_call, r): r for r in visual_slides}
        for fut in as_completed(futures):
            try:
                slide_number, content = fut.result()
            except Exception:
                r = futures[fut]
                log.warning("[%s] slide %d VLM call failed — keeping parser output",
                            label, r.slide_number, exc_info=True)
                continue
            target = by_number.get(slide_number)
            if target is not None:
                _merge_enrichment(target, content, extractor_name=vlm.name)
                n_enriched += 1

    # Post-pass: clean up all slide titles.
    # (a) Slides still untitled: promote first text line as title.
    # (b) Titles with a leading bullet "- FOO" from python-pptx shape: strip the dash.
    for s in slides:
        if not (s.title or "").strip() and s.text.strip():
            first_line = s.text.split("\n")[0].strip()
            clean = re.sub(r"^-\s+", "", first_line).strip()
            if clean:
                s.title = clean
                s.text = s.text[len(first_line):].strip()
        elif s.title:
            s.title = re.sub(r"^-\s+", "", s.title.strip())

    log.info("[%s] Qwen enriched %d visual slides; generating summary ...", label, n_enriched)

    # Build summary digest — InternVL has 8K context; the prompt overhead is ~400 tokens,
    # leaving ~7600 tokens (~3000 chars at ~4 chars/token) for the slide digest text.
    # No images in the summary call: the digest already fills the budget.
    _LOCAL_DIGEST_MAX_CHARS = 3000
    _LOCAL_DIGEST_SLIDE_CHARS = max(100, _LOCAL_DIGEST_MAX_CHARS // max(1, len(slides)))
    parts: list[str] = []
    for s in slides:
        head = f"Slide {s.slide_number}: {s.title or ''}".strip()
        body = "\n".join(p for p in (s.text, *s.tables, s.visuals) if p.strip())[:_LOCAL_DIGEST_SLIDE_CHARS]
        parts.append(f"{head}\n{body}".strip())
    digest = "\n\n".join(parts)[:_LOCAL_DIGEST_MAX_CHARS]

    summary = None
    if digest.strip():
        try:
            t0 = time.time()
            summary = vlm.summarize(digest, images=None) or None
            log.info("[%s] summary done in %.1fs", label, time.time() - t0)
        except Exception:
            log.warning("[%s] summarize failed", label, exc_info=True)

    return DeckExtraction(
        document_id="comparison",
        source_s3_key="local",
        summary=summary,
        slides=slides,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Qwen VLM slide extraction test")
    parser.add_argument("--url", required=True, help="Direct URL to the PPTX file")
    parser.add_argument("--model", required=True, help="vLLM model name")
    parser.add_argument("--vllm-url", default="http://localhost:8000")
    parser.add_argument("--out", required=True, help="Output markdown file")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--dpi", type=int, default=110)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--max-slides", type=int, default=None,
                        help="Only process first N slides (default: all)")
    parser.add_argument("--workers", type=int, default=16,
                        help="Parallel VLM workers (default 16)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )

    label = Path(args.out).stem
    pptx_bytes = download_pptx(args.url)

    vlm = QwenVLM(
        model=args.model,
        base_url=args.vllm_url,
        max_tokens=args.max_tokens,
        timeout_s=args.timeout,
    )

    deck = extract_deck(pptx_bytes, vlm, label=label, dpi=args.dpi,
                        max_slides=args.max_slides, workers=args.workers)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Collapse runs of 3+ blank lines to 2 — VLMs sometimes emit floods of newlines
    markdown = re.sub(r"\n{3,}", "\n\n", deck.to_markdown())
    out_path.write_text(markdown, encoding="utf-8")

    n_enriched = sum(1 for s in deck.slides if s.escalated)
    print(f"\nDone. {len(deck.slides)} slides, {n_enriched} visual slides sent to Qwen.")
    print(f"Output: {out_path}")


if __name__ == "__main__":
    main()
