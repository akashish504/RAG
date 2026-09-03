"""LLMContentNormalizer — extracts structured text from PPTX, PDF, and images.

Sends files directly to Claude without a parser pre-step:
  PDF    → Claude native document block (sees everything including images/charts)
  Images → Claude vision block
  PPTX   → LibreOffice converts to PDF → Claude native document block
            Fallback when LibreOffice is absent: python-pptx text extraction

Output uses ``## Slide N: Title`` for presentations and ``##`` / ``###`` for PDFs
so downstream chunkers can detect structure.

Configuration (environment):
  CONTENT_NORMALIZER_MODEL          — default claude-haiku-4-5-20251001
                                      (faster/cheaper; set to claude-sonnet-4-6
                                      for max fidelity on chart/diagram-heavy docs)
  CONTENT_NORMALIZER_MAX_OUTPUT_TOKENS — default 32768
  CONTENT_NORMALIZER_PAGE_BATCH_SIZE  — default 10
  CONTENT_NORMALIZER_SINGLE_CALL_MAX_PAGES — default 10 (PDFs with fewer pages → one call)
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_MAX_OUTPUT_TOKENS = 32_768
_DEFAULT_PAGE_BATCH_SIZE = 10
_DEFAULT_SINGLE_CALL_MAX_PAGES = 10

_PDF_EXTENSIONS = frozenset({".pdf"})
_PPTX_EXTENSIONS = frozenset({".pptx", ".ppt"})
_DOCX_EXTENSIONS = frozenset({".docx", ".doc"})
_XLSX_EXTENSIONS = frozenset({".xlsx", ".xlsm"})
_XLSX_MAX_ROWS = 2000  # cap per sheet so a giant workbook can't blow up the text
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_IMAGE_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)
_DOC_TITLE_RE = re.compile(r"^DOCUMENT_TITLE:\s*(.+)$", re.MULTILINE)
# Sections to strip from normalized output — pure navigation, no retrieval value.
_STRIP_SECTION_RE = re.compile(
    r"^#{1,3}\s+(?:table\s+of\s+contents|table\s+of\s+figures|list\s+of\s+figures"
    r"|list\s+of\s+tables|contents)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _post_process(text: str) -> str:
    """Remove navigation-only sections and deduplicate running document-title headers."""
    if not text:
        return text

    # Split into sections on heading boundaries.
    sections: list[str] = re.split(r"(?m)(?=^#{1,3}\s)", text)
    kept: list[str] = []
    # Track how many times each exact level-1 heading has appeared.
    # A level-1 heading seen more than once is a running page header — keep only the first.
    h1_seen: dict[str, int] = {}
    for section in sections:
        first_line = section.split("\n", 1)[0].strip()
        # Drop pure navigation sections entirely.
        if _STRIP_SECTION_RE.match(first_line):
            continue
        # Deduplicate repeated level-1 headings (running document title).
        if first_line.startswith("# ") and not first_line.startswith("## "):
            h1_seen[first_line] = h1_seen.get(first_line, 0) + 1
            if h1_seen[first_line] > 1:
                # Strip the heading line but keep the body (may have real content).
                body = section.split("\n", 1)[1] if "\n" in section else ""
                if body.strip():
                    kept.append(body)
                continue
        kept.append(section)

    result = "".join(kept)
    # Collapse 4+ consecutive blank lines to 2.
    result = re.sub(r"\n{4,}", "\n\n\n", result)
    return result.strip()


@dataclass(frozen=True, slots=True)
class ContentNormalizerSettings:
    model: str
    max_output_tokens: int
    page_batch_size: int
    single_call_max_pages: int


def get_content_normalizer_settings() -> ContentNormalizerSettings:
    """Read normalizer settings from the environment (with quality-first defaults)."""

    return ContentNormalizerSettings(
        model=os.environ.get("CONTENT_NORMALIZER_MODEL", _DEFAULT_MODEL).strip()
        or _DEFAULT_MODEL,
        max_output_tokens=int(
            os.environ.get("CONTENT_NORMALIZER_MAX_OUTPUT_TOKENS", str(_DEFAULT_MAX_OUTPUT_TOKENS))
        ),
        page_batch_size=int(
            os.environ.get("CONTENT_NORMALIZER_PAGE_BATCH_SIZE", str(_DEFAULT_PAGE_BATCH_SIZE))
        ),
        single_call_max_pages=int(
            os.environ.get(
                "CONTENT_NORMALIZER_SINGLE_CALL_MAX_PAGES",
                str(_DEFAULT_SINGLE_CALL_MAX_PAGES),
            )
        ),
    )


_SYSTEM_EXTRACTOR = (
    "You are an expert document content extractor for a professional knowledge library. "
    "Your job is to produce a comprehensive, retrieval-friendly plain-text representation "
    "of consulting research briefs, policy reports, strategy decks, proposals, and data presentations.\n\n"
    "Content you MUST extract and never skip:\n"
    "- Document hierarchy: titles, headings, subheadings, sections, and logical flow\n"
    "- Every bullet at every nesting level (preserve indentation as dashes or numbers)\n"
    "- Every table: all headers, rows, columns, and cells — including empty cells\n"
    "- Every chart/graph: title, axes, units, legend, and every readable data point\n"
    "- Every diagram/framework: all nodes, labels, connectors, layers, and legend text\n"
    "- Every block diagram (shapes/boxes with text): ALL text inside EVERY shape verbatim — use BLOCK_DIAGRAM block\n"
    "- Every multi-panel figure: treat each sub-panel as a separate labelled chart or diagram\n"
    "- Every icon/symbol-coded matrix (dot ratings, traffic lights, scorecards): decode the symbol key\n"
    "- Every data map (choropleth / regional shading / pins): extract each region→value pair and the legend scale — never just 'a map is shown'\n"
    "- Every timeline / roadmap / Gantt / phasing chart: extract each milestone or phase with its date, range, or sequence\n"
    "- Any equation or formula: transcribe it in readable notation (e.g. CAGR = (End/Start)^(1/n) − 1)\n"
    "- Every case study, callout box, or highlighted sidebar: full verbatim text\n"
    "- Every footnote: full text keyed to its superscript number\n"
    "- Callouts, annotations, headers, footers, page numbers, sources, disclaimers\n"
    "- All named entities and all numbers (with units) exactly as shown\n\n"
    "Visual content is first-class — do not replace visuals with vague descriptions.\n"
    "For each chart: after the Data block add INSIGHTS: trends, comparisons, rankings, "
    "outliers, and correlations that are visibly supported by the data (no invented numbers).\n"
    "For each diagram/framework: add STRUCTURE: (hierarchy/layers) and RELATIONSHIPS: "
    "(flows, dependencies, causal links) grounded in what is shown.\n"
    "For multi-panel figures: extract each panel in order with its sub-title or panel label.\n"
    "For icon/symbol matrices: always output the Symbol key before the data rows.\n\n"
    "Rules:\n"
    "- Reproduce text verbatim; describe visuals in structured blocks (see block types below)\n"
    "- Do NOT fabricate data, statistics, labels, or series that are not in the document. BUT if a "
    "chart value is unlabeled yet readable from axis gridlines, estimate it and mark it approximate "
    "with '~' (e.g. ~90) rather than dropping it\n"
    "- Do NOT truncate tables, chart series, bullet lists, or footnote text\n"
    "- Avoid useless phrases like 'a chart is shown' or 'a diagram is present' — extract the actual information\n"
    "- Section-divider pages (full-page photo + section title only): output the section title and move on\n"
    "- SKIP Table of Contents and Table of Figures pages entirely — do not extract them\n"
    "- SKIP running page headers that repeat the document title — do not output them as headings\n"
    "- If a figure or table spans multiple pages: extract it once fully on the first page it appears; "
    "on continuation pages write only 'CONTINUED: [Figure/Table title]' then carry on with new content\n"
    "- MATRIX blocks: always output the Symbol key AND the actual value (symbol) for every row × column combination"
)


def _context_prefix(carry_forward: str | None) -> str:
    if not carry_forward or not carry_forward.strip():
        return ""
    return (
        "CONTEXT (from earlier pages — do not re-extract these pages; use for continuity only):\n"
        f"{carry_forward.strip()}\n\n"
    )


def _pptx_batch_prompt(
    start_slide: int,
    end_slide: int,
    *,
    carry_forward: str | None = None,
    is_first_batch: bool = True,
) -> str:
    title_line = (
        "On the first slide of this batch, if a clear deck title is visible, "
        "output one line: DOCUMENT_TITLE: [exact title]\n"
        if is_first_batch
        else ""
    )
    return (
        _context_prefix(carry_forward)
        + f"""\
These are slides {start_slide} to {end_slide} of a PowerPoint presentation.
Extract ALL content from EVERY slide with complete fidelity. Miss nothing.
{title_line}
For EACH slide output this structure (omit a section only if entirely absent):

## Slide N: [Slide Title or "(untitled)"]

TEXT CONTENT:
[Every line of text — titles, subtitles, bullets, captions. Nested bullets: "  -", "    -", etc.
If text AND visuals coexist, output text here first, then visual blocks below.]

TABLE: [title if visible]
| [Header 1] | [Header 2] | ... |
|---|---|...|
| [all rows] |
Include ALL rows/columns. Multiple tables → separate TABLE: blocks.

MULTI_PANEL_FIGURE: [title] ([N] panels)
  Panel [label] — [panel title]:
  [CHART, DIAGRAM, or TABLE block for that panel]
Use when a single slide element contains multiple sub-charts or sub-panels.

CHART: [title]
Type: [bar / line / pie / scatter / waterfall / stacked / radar / area / other]
X-axis: [label and unit]
Y-axis: [label and unit]
Series: [legend entries]
Data:
  [Series] — [label]: [value], ... (every readable point)
Key annotations: [on-chart labels, callouts]
INSIGHTS: [trends, comparisons, rankings, outliers — only from visible data]

MAP: [title]
Type: [choropleth / regional shading / pins / flow]
Measure: [what the colour/size encodes, with unit]
Legend scale: [bins or gradient → value ranges]
Data:
  [Region/Country] : [value or legend bin], ... (every labelled region)

TIMELINE: [title]
Type: [roadmap / Gantt / phases / milestones]
Items:
  [Phase or Milestone] — [date, range, or sequence]: [short description if shown]

MATRIX: [title]
Purpose: [what the matrix assesses]
Row groups: [outer row categories if present]
Column labels: [Col1] | [Col2] | [Col3] ...
Symbol key: [every symbol → its meaning, e.g. ●●● = High, ●●○ = Medium, ●○○ = Low, n.a. = not applicable]
Data — MANDATORY: one line per row, ACTUAL SYMBOL for every column, no omissions.
  Format: [Row label] | [Col1 symbol] | [Col2 symbol] | [Col3 symbol]
  Example: LC financing | ●●● | ●●● | ●●○

BLOCK_DIAGRAM: [title or type — org chart / process flow / Venn / pyramid / swim-lane / framework / etc.]
Purpose: [what this diagram is communicating]
Layout: [linear / hierarchical / circular / grid / swim-lane / other]
Boxes (extract the COMPLETE verbatim text from EVERY shape, box, icon, or label):
  Box 1: [exact full text inside this shape — never truncate, never paraphrase]
  Box 2: [exact full text]
  Box 3: [exact full text]
  ... (one entry per shape; number them so connections can reference them)
CONNECTIONS: [Box N → Box M (connector label if present), one per line]
Legend: [colour, shape, or size coding]
Use for ANY slide where text lives inside shapes, boxes, icons, arrows, or swimlane cells.

DIAGRAM: [title or type — for spatial/conceptual visuals without discrete text-boxes]
STRUCTURE: [layers, phases, groupings, axes]
Elements: [every labeled element, one per line]
RELATIONSHIPS: [directed flows: "A → B (label)"]
Legend: [colour/spatial coding]

CASE_STUDY: [title]
[Full verbatim text including all numbers, organizations, countries, dates, outcomes]

CALLOUT: [full text per callout box or pull-quote]

VISUAL: [photos/maps/icons — subject and embedded text; skip purely decorative images]

Notes: [speaker notes verbatim, if present]

Footer: [source attribution only — omit running title headers]

After ALL content for each slide output exactly this line and nothing else:
[Page N]
where N equals that slide's number. Format must be exactly "[Page N]" — no other text on that line.

Global rules:
- First slide in this batch = Slide {start_slide} (= Page {start_slide})
- Untitled slides: ## Slide N: (untitled)
- Every slide needs ## Slide N: at the top AND [Page N] as its final line
- Do not repeat slides from CONTEXT; continue numbering from {start_slide}
- BLOCK_DIAGRAM takes priority over DIAGRAM whenever shapes contain text
- If a visual element spans multiple slides, extract it once fully on the first slide; write CONTINUED: [title] on subsequent slides — never re-extract from the beginning
- Each body text block and each visual element appears ONCE across all slides in this batch
"""
    )


def _pdf_batch_prompt(
    start_page: int,
    end_page: int,
    *,
    carry_forward: str | None = None,
    is_first_batch: bool = True,
    is_single_call: bool = False,
) -> str:
    scope = (
        "this entire PDF document"
        if is_single_call
        else f"pages {start_page} to {end_page} of a PDF document"
    )
    title_line = (
        "If a clear document title appears on the cover or header, output: DOCUMENT_TITLE: [exact title]\n"
        if is_first_batch
        else ""
    )
    continuation = (
        ""
        if is_single_call
        else (
            f"- Continue from page {start_page}; do not re-output content from CONTEXT pages\n"
            f"- If a section/table started before page {start_page}, continue it without repeating headers already given in CONTEXT\n"
        )
    )
    return (
        _context_prefix(carry_forward)
        + f"""\
Extract ALL content from {scope} with complete fidelity. Miss nothing.
{title_line}
STRUCTURE:
- Use ## for major sections and ### for sub-sections (verbatim headings)
- Two-column layouts: read left column fully, then right column
- Body text verbatim; nested bullets with "  -" and "    -"

TABLE: [title if visible]
| [headers] |
|---|---|
| [all rows] |
All rows/columns. Spanning tables: include every row visible on these pages.

MULTI_PANEL_FIGURE: [figure number and title] ([N] panels)
  Panel [label or number] — [panel title]:
  [Full CHART, DIAGRAM, or TABLE block for this panel]
  Panel [label or number] — [panel title]:
  [...]
Use for any numbered figure that contains multiple sub-charts, sub-graphs, or sub-panels.

CHART: [title]
Type: [bar / line / pie / scatter / waterfall / stacked / radar / area / box / other]
X-axis: [label and unit]
Y-axis: [label and unit]
Series: [legend entries]
Data:
  [Series] — [label]: [value], ... (every readable point)
Key annotations: [on-chart callouts and labels]
INSIGHTS: [trends, comparisons, rankings, outliers — only from visible data]

MAP: [title]
Type: [choropleth / regional shading / pins / flow]
Measure: [what the colour/size/shade encodes, with unit]
Legend scale: [bins or gradient → value ranges]
Data:
  [Region/Country] : [value or legend bin], ... (every labelled region)
INSIGHTS: [highest / lowest regions, clusters — only from visible data]

TIMELINE: [title]
Type: [roadmap / Gantt / phases / milestones]
Items:
  [Phase or Milestone] — [date, range, or sequence]: [short description if shown]

MATRIX: [title]
Purpose: [what the matrix is assessing or comparing]
Row groups: [outer row categories if present, e.g. REDUCTION / MITIGATION]
Column labels: [Col1] | [Col2] | [Col3] ...
Symbol key: [every symbol → its exact meaning, e.g. ●●● = High relevance, ●●○ = Medium, ●○○ = Low, n.a. = not applicable]
Data — MANDATORY: output one line per row with the ACTUAL SYMBOL for every column.
  Never omit a row. Never omit a column value. Never write "see above" or leave blank.
  Format: [Row label] | [Col1 symbol] | [Col2 symbol] | [Col3 symbol]
  Example: LC financing | ●●● | ●●● | ●●○
Use for icon-coded, dot-rated, traffic-light, or scorecard tables where symbols carry meaning.

BLOCK_DIAGRAM: [title or type — org chart / process flow / Venn / pyramid / framework / etc.]
Purpose: [what this diagram is communicating]
Layout: [linear / hierarchical / circular / grid / other]
Boxes (extract COMPLETE verbatim text from EVERY shape, box, or label — never paraphrase):
  Box 1: [exact full text inside this shape]
  Box 2: [exact full text]
  Box 3: [exact full text]
  ... (number every shape)
CONNECTIONS: [Box N → Box M (connector label if present), one per line]
Legend: [colour, shape, or size coding]
Use for any visual where information lives inside shapes, boxes, or swimlane cells.

DIAGRAM: [title or type — framework / process / matrix / map / risk framework]
STRUCTURE: [layers, phases, groupings, axes]
Elements: [every labeled node, shape, cell, or phase — one per line]
RELATIONSHIPS: [directed flows and dependencies: "A → B (label)"]
Legend: [colour, size, or spatial coding]

CASE_STUDY: [title]
[Full verbatim text of the case study box including all numbers, organizations,
countries, dates, project names, financial figures, and outcomes]

CALLOUT: [full text of callout box or highlighted pull-quote]

VISUAL: [full-page or decorative photos — subject only; skip if purely decorative]

FOOTNOTE [N]: [complete footnote text as it appears at the bottom of the page]
(One block per footnote; N matches the superscript number in the body text)

Footer: [source attribution line only — omit running headers that repeat the document title]

After ALL content for each individual page output exactly this line and nothing else:
[Page N]
where N is the document page number printed on that page.
This line is MANDATORY for every page. Format must be exactly "[Page N]" — no other text on that line.

Rules:
- Preserve all numbers, names, dates, and units exactly as written
- Do not fabricate data; but read approximate values off axis gridlines (mark with ~) rather than dropping them
- Do not truncate tables, chart series, case study text, or footnotes
- Section-divider pages (photo + section title only): output ## [Section title] then [Page N]
- BLOCK_DIAGRAM takes priority over DIAGRAM whenever shapes contain text
- SKIP Table of Contents and Table of Figures pages — output only [Page N] for those pages
- If a figure/table started on a previous page, write CONTINUED: [title] and continue; do not re-extract from the beginning
- Two-column layouts: each body paragraph appears ONCE — do not re-extract a paragraph that already appeared earlier on the same page or a previous page in this batch
- If you see the same paragraph or figure on multiple pages of this batch, output it once on the first page it appears and skip it on later pages
{continuation}
"""
    )


_IMAGE_PROMPT = """\
Extract ALL content from this image completely and faithfully.

- Transcribe all visible text verbatim
- TABLE: markdown with every row and column
- MULTI_PANEL_FIGURE: label each panel and extract separately
- CHART: Type, axes, Series, Data (every point), INSIGHTS (visible trends only)
- MAP: Type, Measure, Legend scale, Data (every region→value), INSIGHTS
- TIMELINE: Type, Items (each milestone/phase → date or sequence)
- MATRIX: Purpose, Row labels, Column labels, Symbol key, Data per row
- BLOCK_DIAGRAM: ALL text inside every shape/box verbatim (numbered), CONNECTIONS, Legend
- DIAGRAM: STRUCTURE, Elements, RELATIONSHIPS, Legend (for non-box visuals)
- CASE_STUDY: full verbatim text
- CALLOUT: full text
- VISUAL: subject and embedded text
- FOOTNOTE N: full footnote text
- Footer/caption/source: verbatim

Do not fabricate numbers or labels; read approximate chart values off gridlines (mark with ~) rather than dropping them.
"""


def _split_pdf_into_batches(
    pdf_bytes: bytes,
    batch_size: int,
) -> list[tuple[bytes, int, int]]:
    """Split PDF into page batches.

    Returns list of (batch_pdf_bytes, start_page_1indexed, end_page_1indexed).
    """
    from pypdf import PdfReader, PdfWriter  # noqa: PLC0415

    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    batches: list[tuple[bytes, int, int]] = []
    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        writer = PdfWriter()
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        batches.append((buf.getvalue(), start + 1, end))
    return batches


def _pdf_page_count(pdf_bytes: bytes) -> int:
    from pypdf import PdfReader  # noqa: PLC0415

    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def _build_carry_forward_context(batch_output: str, *, end_page: int) -> str:
    """Summarize tail of a batch for the next batch's CONTEXT block."""

    if not batch_output or not batch_output.strip():
        return f"Last completed page/slide: {end_page}"

    title_m = _DOC_TITLE_RE.search(batch_output)
    doc_title = title_m.group(1).strip() if title_m else None

    headings = _HEADING_RE.findall(batch_output)
    last_heading = headings[-1].strip() if headings else None

    tail = batch_output.strip()[-2500:]

    parts: list[str] = []
    if doc_title:
        parts.append(f"Document title: {doc_title}")
    parts.append(f"Last completed page/slide: {end_page}")
    if last_heading:
        parts.append(f"Last open section/slide heading: {last_heading}")
    parts.append(f"Trailing excerpt (for continuity):\n{tail}")
    return "\n".join(parts)


def _xlsx_table_markdown(rows: list[list[str]]) -> str:
    """Render a list of row-cell-lists as a GitHub-markdown table."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    header = (rows[0] + [""] * width)[:width]
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * width) + "|"]
    for r in rows[1:]:
        cells = (r + [""] * width)[:width]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def _process_xlsx(binary: bytes, filename: str) -> str | None:
    """Extract an XLSX/XLSM workbook to markdown — one ``## <sheet>`` + table per
    sheet. Deterministic (openpyxl, no LLM): lossless for tabular content, cheap
    and fast. Empty sheets and all-empty rows are skipped; very large sheets are
    capped at ``_XLSX_MAX_ROWS`` with a note. Returns None if openpyxl is missing
    or the workbook is unreadable, so the original is kept.
    """
    try:
        import openpyxl  # noqa: PLC0415
    except ImportError:
        log.warning("xlsx: openpyxl not installed — skipping %r", filename)
        return None

    import io  # noqa: PLC0415
    import warnings  # noqa: PLC0415

    # openpyxl emits noisy UserWarnings for Excel features it doesn't model
    # (conditional formatting, data validation, custom extensions) — during BOTH
    # load and lazy row iteration. They're harmless (cell values still read fine),
    # so silence them across the whole read.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        try:
            wb = openpyxl.load_workbook(io.BytesIO(binary), read_only=True, data_only=True)
        except Exception:  # noqa: BLE001
            log.warning("xlsx: could not read %r", filename, exc_info=True)
            return None

        sections: list[str] = []
        for ws in wb.worksheets:
            rows: list[list[str]] = []
            truncated = False
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= _XLSX_MAX_ROWS:
                    truncated = True
                    break
                cells = ["" if c is None else " ".join(str(c).split()) for c in row]
                if any(cells):  # skip fully empty rows
                    rows.append(cells)
            if not rows:
                continue
            block = [f"## {ws.title}", _xlsx_table_markdown(rows)]
            if truncated:
                block.append(f"[Sheet truncated at {_XLSX_MAX_ROWS} rows]")
            sections.append("\n".join(block))

        wb.close()

    if not sections:
        log.info("xlsx: %r had no tabular content", filename)
        return None
    log.info("xlsx: %r → %d sheet(s)", filename, len(sections))
    return "\n\n".join(sections)


class LLMContentNormalizer:
    """Normalize PPTX, PDF, and image attachments via direct Claude API calls.

    PDFs/PPTXs with fewer than ``single_call_max_pages`` pages are sent in one
    Claude call for maximum context retention. Longer documents are batched
    sequentially with carry-forward context between batches.
    """

    def __init__(self, *, api_key: str, settings: ContentNormalizerSettings | None = None) -> None:
        self._api_key = api_key
        self._settings = settings or get_content_normalizer_settings()
        self._client = anthropic.Anthropic(api_key=api_key)
        log.info(
            "llm_content: model=%s max_tokens=%d batch_size=%d single_call_max_pages=%d",
            self._settings.model,
            self._settings.max_output_tokens,
            self._settings.page_batch_size,
            self._settings.single_call_max_pages,
        )

    def normalize(self, binary: bytes, filename: str) -> str | None:
        ext = Path(filename).suffix.lower()
        try:
            if ext in _PDF_EXTENSIONS:
                return self._process_pdf(binary, filename)
            if ext in _PPTX_EXTENSIONS:
                return self._process_pptx(binary, filename)
            if ext in _DOCX_EXTENSIONS:
                return self._process_docx(binary, filename)
            if ext in _XLSX_EXTENSIONS:
                return _process_xlsx(binary, filename)
            if ext in _IMAGE_EXTENSIONS:
                return self._process_image(binary, filename, ext)
            return None
        except Exception:
            log.warning(
                "llm_content: error processing %r — falling back to original",
                filename,
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # File-type handlers
    # ------------------------------------------------------------------

    def _process_pdf(self, binary: bytes, filename: str) -> str | None:
        page_count = _pdf_page_count(binary)
        max_single = self._settings.single_call_max_pages

        if page_count <= max_single:
            log.info(
                "llm_content: PDF %r — %d pages, single-call extraction",
                filename,
                page_count,
            )
            prompt = _pdf_batch_prompt(
                1,
                page_count,
                is_first_batch=True,
                is_single_call=True,
            )
            result = self._call_claude_pdf(binary, prompt=prompt)
            return _post_process(result) if result else result

        batch_size = self._settings.page_batch_size
        batches = _split_pdf_into_batches(binary, batch_size)
        log.info(
            "llm_content: PDF %r — %d pages → %d batch(es) of up to %d pages",
            filename,
            page_count,
            len(batches),
            batch_size,
        )
        return self._run_batches_sequential(batches, filename, is_pptx=False)

    def _call_claude_pdf(self, pdf_bytes: bytes, *, prompt: str) -> str | None:
        content: list[dict] = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(pdf_bytes).decode(),
                },
            },
            {"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}},
        ]
        return self._call_claude(content)

    def _process_pptx(self, binary: bytes, filename: str) -> str | None:
        from pipeline.preprocessing.normalizers.pptx_converter import pptx_to_pdf_bytes  # noqa: PLC0415

        pdf_bytes = pptx_to_pdf_bytes(binary)
        if pdf_bytes is not None:
            return self._process_pptx_pdf(pdf_bytes, filename)

        log.info("llm_content: PPTX fallback (python-pptx text) for %r", filename)
        return self._process_pptx_text_fallback(binary, filename)

    def _process_pptx_pdf(self, pdf_bytes: bytes, filename: str) -> str | None:
        page_count = _pdf_page_count(pdf_bytes)
        max_single = self._settings.single_call_max_pages

        if page_count <= max_single:
            log.info(
                "llm_content: PPTX %r — %d slides (as PDF), single-call extraction",
                filename,
                page_count,
            )
            prompt = _pptx_batch_prompt(1, page_count, is_first_batch=True)
            return self._call_claude_pdf(pdf_bytes, prompt=prompt)

        batch_size = self._settings.page_batch_size
        batches = _split_pdf_into_batches(pdf_bytes, batch_size)
        log.info(
            "llm_content: PPTX %r — %d slides → %d batch(es)",
            filename,
            page_count,
            len(batches),
        )
        return self._run_batches_sequential(batches, filename, is_pptx=True)

    def _process_docx(self, binary: bytes, filename: str) -> str | None:
        """Convert DOCX/DOC to PDF via LibreOffice, then process as PDF.

        Falls back to None (raw binary uploaded unchanged) when LibreOffice
        is unavailable — the embedding pipeline's DocxParser will handle it
        with text-only extraction.
        """
        from pipeline.preprocessing.normalizers.pptx_converter import pptx_to_pdf_bytes  # noqa: PLC0415

        pdf_bytes = pptx_to_pdf_bytes(binary)
        if pdf_bytes is None:
            log.info(
                "llm_content: DOCX %r — LibreOffice unavailable, skipping LLM normalisation",
                filename,
            )
            return None
        log.info("llm_content: DOCX %r → PDF → Claude", filename)
        return self._process_pdf(pdf_bytes, filename)

    def _run_batches_sequential(
        self,
        batches: list[tuple[bytes, int, int]],
        filename: str,
        *,
        is_pptx: bool,
    ) -> str | None:
        """Process batches in order with carry-forward context between batches."""

        results: list[str] = []
        carry_forward: str | None = None

        for idx, (batch_bytes, start, end) in enumerate(batches):
            is_first = idx == 0
            if is_pptx:
                prompt = _pptx_batch_prompt(
                    start, end, carry_forward=carry_forward, is_first_batch=is_first
                )
            else:
                prompt = _pdf_batch_prompt(
                    start, end, carry_forward=carry_forward, is_first_batch=is_first
                )
            try:
                text = self._call_claude_pdf(batch_bytes, prompt=prompt)
            except Exception:
                log.warning(
                    "llm_content: %r — batch pages %d–%d failed after retries, skipping",
                    filename,
                    start,
                    end,
                    exc_info=True,
                )
                text = None

            if text:
                results.append(text)
                carry_forward = _build_carry_forward_context(text, end_page=end)

        combined = "\n\n".join(results) if results else None
        return _post_process(combined) if combined else None

    def _process_image(self, binary: bytes, filename: str, ext: str) -> str | None:
        media_type = _IMAGE_MEDIA_TYPES.get(ext, "image/png")
        log.info("llm_content: image→Claude for %r (%d bytes)", filename, len(binary))
        content: list[dict] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(binary).decode(),
                },
            },
            {"type": "text", "text": _IMAGE_PROMPT, "cache_control": {"type": "ephemeral"}},
        ]
        return self._call_claude(content)

    def _process_pptx_text_fallback(self, binary: bytes, filename: str) -> str | None:
        """python-pptx text extraction fallback (no LibreOffice).

        Produces ``## Slide N: Title`` output compatible with PPTXSlideChunker
        without an additional LLM call — text quality is limited to what
        python-pptx can extract (no visual content).
        """
        try:
            from pptx import Presentation  # noqa: PLC0415
        except ImportError:
            log.warning("llm_content: python-pptx not installed, cannot process %r", filename)
            return None

        prs = Presentation(io.BytesIO(binary))
        slide_blocks: list[str] = []

        for slide_idx, slide in enumerate(prs.slides, start=1):
            title_text = ""
            if slide.shapes.title:
                title_text = (slide.shapes.title.text or "").strip()

            parts: list[str] = []
            for shape in slide.shapes:
                if not getattr(shape, "has_text_frame", False):
                    continue
                block = (shape.text_frame.text or "").strip()
                if block and block != title_text:
                    parts.append(block)

            notes = ""
            try:
                if slide.has_notes_slide:
                    notes = (slide.notes_slide.notes_text_frame.text or "").strip()
            except Exception:  # noqa: BLE001
                pass

            heading = f"## Slide {slide_idx}: {title_text or '(untitled)'}"
            body_lines: list[str] = list(filter(None, parts))
            if notes:
                body_lines.append(f"Notes: {notes}")

            block = heading + ("\n" + "\n".join(body_lines) if body_lines else "")
            slide_blocks.append(block)

        if not slide_blocks:
            return None
        return "\n\n".join(slide_blocks)

    # ------------------------------------------------------------------
    # Claude API call with retry
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(
            (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError)
        ),
        wait=wait_random_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    def _call_claude(self, content: list[dict]) -> str:
        # Use streaming for all calls — the SDK requires it when max_tokens is high
        # enough that the response could take over 10 minutes.  get_final_message()
        # waits for the stream to complete and returns the same Message object as
        # the non-streaming API, so usage/stop_reason are still available.
        with self._client.messages.stream(
            model=self._settings.model,
            max_tokens=self._settings.max_output_tokens,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_EXTRACTOR,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": content}],
        ) as stream:
            msg = stream.get_final_message()
        usage = msg.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        log.info(
            "llm_content: tokens — input=%d (cache_read=%d cache_write=%d) output=%d stop=%s",
            usage.input_tokens,
            cache_read,
            cache_write,
            usage.output_tokens,
            msg.stop_reason,
        )
        if msg.stop_reason == "max_tokens":
            log.warning(
                "llm_content: OUTPUT TRUNCATED — increase CONTENT_NORMALIZER_MAX_OUTPUT_TOKENS "
                "or reduce CONTENT_NORMALIZER_PAGE_BATCH_SIZE (current: %d tokens, %d pages/batch)",
                self._settings.max_output_tokens,
                self._settings.page_batch_size,
            )
        return msg.content[0].text
