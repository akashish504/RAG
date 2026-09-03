"""Render a PPTX into per-slide images + shape stats for the tiered extractor.

Pipeline: PPTX --(LibreOffice)--> PDF --(PyMuPDF)--> per-page PNG, zipped with
python-pptx-derived speaker notes and a cheap shape inventory used to classify
each slide (which drives the Haiku escalation gate).

``classify_from_stats`` is a pure function (unit-tested); the rendering itself
needs LibreOffice + PyMuPDF + python-pptx at runtime.
"""

from __future__ import annotations

import io
import logging
import os

from pipeline.preprocessing.slides.extractor import RenderedSlide
from pipeline.preprocessing.slides.models import SlideClass

log = logging.getLogger(__name__)

# Slide-render DPI. Lower = fewer image tokens (cheaper Haiku calls) + lighter
# PyMuPDF rasterization. 110 keeps chart labels legible while cutting ~25-30% of
# image tokens vs 150. Tunable per run via RENDER_DPI (e.g. 96 for max savings).
_RENDER_DPI = int(os.environ.get("RENDER_DPI", "110"))


def classify_from_stats(stats: dict) -> SlideClass:
    """Classify a slide from its python-pptx shape inventory (no LLM).

    Drives the escalation gate: only visual classes (FRAMEWORK/DIAGRAM/MIXED/IMAGE)
    are eligible for Haiku.
    """
    text_chars = int(stats.get("text_chars", 0))
    has_table = bool(stats.get("has_table"))
    has_chart = bool(stats.get("has_chart"))
    has_picture = bool(stats.get("has_picture"))
    has_group = bool(stats.get("has_group"))  # SmartArt / grouped shapes ≈ framework/diagram
    n_shapes = int(stats.get("n_shapes", 0))

    if text_chars == 0 and n_shapes == 0:
        return "EMPTY"
    if has_chart:
        # A chart plus lots of surrounding text is a MIXED analysis slide.
        return "MIXED" if text_chars > 200 else "DIAGRAM"
    if has_group:
        return "FRAMEWORK"
    if has_picture and text_chars < 120:
        return "IMAGE"
    if has_picture:
        return "MIXED"
    if has_table and text_chars < 80:
        return "TABLE"
    return "TEXT"


# A picture on a slide that already carries plenty of parsed text is almost always
# decoration (logo, header graphic, background) — branded decks put one on EVERY
# slide. Only escalate a picture to the vision model when the slide is
# picture-DOMINANT (little parsed text), i.e. the image likely *is* the content
# (screenshot / infographic / diagram-as-image). Charts and SmartArt/groups are
# never readable by the parser, so they always escalate.
_PICTURE_TEXT_FLOOR = 200  # chars of parsed text above which a lone picture is decoration


def is_visual_from_stats(stats: dict) -> bool:
    """True when the slide has content the text parser can't read (→ needs an image).

    Charts and grouped/SmartArt shapes always qualify. A picture qualifies only on
    a picture-dominant slide; a logo/graphic on a text-rich slide does not, so we
    don't ship every branded slide to the vision model.
    """
    if stats.get("has_chart") or stats.get("has_group"):
        return True
    if stats.get("has_picture") and int(stats.get("text_chars", 0)) < _PICTURE_TEXT_FLOOR:
        return True
    return False


# Parser-quality gate thresholds (used by needs_image).
_THIN_MIN_SHAPE_CHARS = 40    # below this the slide is too small to judge coverage
_THIN_COVERAGE_FLOOR = 0.5    # parser must capture at least half the shapes' text


def needs_image(stats: dict, parsed_text: str, parsed_tables: list[str]) -> bool:
    """Parser-quality gate: True when the parser likely missed text on a slide.

    Visual slides are already covered by :func:`is_visual_from_stats`; this catches
    the case where the shape inventory reports real text but the parser extracted
    far less (text trapped in odd shapes, art text, broken runs) — so we don't
    blindly ship thin parser output without letting the vision model double-check.
    """
    stat_chars = int(stats.get("text_chars", 0))
    n_shapes = int(stats.get("n_shapes", 0))
    parsed_chars = len((parsed_text or "").strip()) + sum(
        len((t or "").strip()) for t in (parsed_tables or [])
    )
    # Shapes report meaningful text but the parser captured well under half of it.
    if stat_chars >= _THIN_MIN_SHAPE_CHARS and parsed_chars < max(
        20, int(_THIN_COVERAGE_FLOOR * stat_chars)
    ):
        return True
    # Slide has shapes with text, but the parser produced essentially nothing.
    if n_shapes > 0 and stat_chars > 0 and parsed_chars == 0:
        return True
    return False


def _collect_shape_stats(slide) -> tuple[dict, str | None]:
    """Return (shape_stats, notes) for one python-pptx slide."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE  # noqa: PLC0415

    text_chars = 0
    has_table = has_chart = has_picture = has_group = False
    n_shapes = 0
    for shape in slide.shapes:
        n_shapes += 1
        if getattr(shape, "has_text_frame", False):
            text_chars += len((shape.text_frame.text or "").strip())
        if getattr(shape, "has_table", False):
            has_table = True
        if getattr(shape, "has_chart", False):
            has_chart = True
        stype = getattr(shape, "shape_type", None)
        if stype == MSO_SHAPE_TYPE.PICTURE:
            has_picture = True
        if stype == MSO_SHAPE_TYPE.GROUP:
            has_group = True
        # SmartArt / embedded OLE objects arrive as graphic frames that are
        # neither tables nor charts. python-pptx cannot read their text, so the
        # parser would silently drop it — flag them so the slide goes to the image.
        if (
            type(shape).__name__ == "GraphicFrame"
            and not getattr(shape, "has_table", False)
            and not getattr(shape, "has_chart", False)
        ):
            has_group = True

    notes = None
    try:
        if slide.has_notes_slide:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip() or None
    except Exception:  # noqa: BLE001
        notes = None

    return (
        {
            "text_chars": text_chars,
            "has_table": has_table,
            "has_chart": has_chart,
            "has_picture": has_picture,
            "has_group": has_group,
            "n_shapes": n_shapes,
        },
        notes,
    )


def _text_frame_markdown(text_frame) -> str:
    """Render a python-pptx text frame as markdown bullets/paragraphs.

    Each paragraph becomes a line; indentation follows the paragraph level so
    nested bullets are preserved as ``  -``.
    """
    lines: list[str] = []
    for para in text_frame.paragraphs:
        text = "".join(run.text or "" for run in para.runs).strip()
        if not text:
            # Fall back to the paragraph's flattened text (handles runs-less paras).
            text = (para.text or "").strip()
        if not text:
            continue
        level = int(getattr(para, "level", 0) or 0)
        lines.append(f"{'  ' * level}- {text}")
    return "\n".join(lines)


def _table_markdown(table) -> str:
    """Render a python-pptx table as a GitHub-flavoured markdown table."""
    rows: list[list[str]] = []
    for row in table.rows:
        cells = [(" ".join((c.text or "").split())).strip() for c in row.cells]
        rows.append(cells)
    if not rows:
        return ""
    header, *body = rows
    width = len(header)
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * width) + "|"]
    for r in body:
        # Pad/truncate so every row matches the header width.
        cells = (r + [""] * width)[:width]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def _column_aware_order(shapes: list) -> list:
    """Sort shapes into column-first reading order.

    Slides with side-by-side blocks (e.g. three coloured columns) should be
    read as: all of column-1 top-to-bottom, then all of column-2, etc. A naive
    top-then-left sort reads the first row of every column before the second row
    of any column, which scrambles block-structured slides.

    Algorithm:
      1. Sort shapes left-to-right by their horizontal centre.
      2. Cluster into columns: a shape starts a new column when its left edge is
         more than _COLUMN_GAP_EMU away from the previous shape's right edge.
         Using left/right edges (not centres) handles variable-width blocks.
      3. Within each column sort top-to-bottom.
      4. Columns are emitted left-to-right.

    Falls back to simple top-then-left order when all shapes overlap
    horizontally (i.e. the slide is a single-column layout).
    """
    if not shapes:
        return shapes

    # A standard slide is 9144000 EMU wide (10 inches at 914400 EMU/inch).
    # 5% of slide width ≈ 457000 EMU is a reasonable minimum gap between columns.
    _COLUMN_GAP_EMU = 457000

    def _left(s):
        v = getattr(s, "left", None)
        return int(v) if v is not None else 0

    def _right(s):
        left = _left(s)
        width = getattr(s, "width", None)
        return left + (int(width) if width is not None else 0)

    def _top(s):
        v = getattr(s, "top", None)
        return int(v) if v is not None else 0

    sorted_lr = sorted(shapes, key=_left)

    columns: list[list] = []
    for shape in sorted_lr:
        if not columns or (_left(shape) - _right(columns[-1][-1])) > _COLUMN_GAP_EMU:
            columns.append([shape])
        else:
            columns[-1].append(shape)

    # Single column detected — use plain top-then-left to avoid reordering
    # shapes that simply overlap slightly (e.g. a wide title + a narrow label).
    if len(columns) == 1:
        return sorted(shapes, key=lambda s: (_top(s), _left(s)))

    result: list = []
    for col in columns:
        result.extend(sorted(col, key=_top))
    return result


# Shape names that are navigation chrome repeated on every slide — not content.
# Matched as exact strings (after strip); extend if the deck adds more nav shapes.
_NAV_SHAPE_NAMES: frozenset[str] = frozenset({
    "Subtitle 4",  # nav-tab bar present on every slide in IDH-style decks
})

# Shape types that carry raster/OLE content python-pptx cannot read as text.
# Their bounding boxes are stored so the VLM can be given just those crops.
_RASTER_SHAPE_TYPES: frozenset[int] = frozenset({
    13,  # PICTURE
    7,   # EMBEDDED_OLE_OBJECT  (Excel objects, SmartArt rendered as OLE)
})


def _is_nav_shape(shape) -> bool:
    """True when the shape is a navigation/chrome element, not slide content."""
    return (getattr(shape, "name", "") or "").strip() in _NAV_SHAPE_NAMES


def _collect_visual_regions(slide) -> list[dict]:
    """Return bounding boxes (in EMU) of raster/OLE shapes that need VLM OCR.

    Each entry: {left, top, width, height} in EMU. Nav shapes are excluded.
    """
    from pptx.enum.shapes import MSO_SHAPE_TYPE  # noqa: PLC0415

    regions: list[dict] = []
    for shape in slide.shapes:
        if _is_nav_shape(shape):
            continue
        stype = getattr(shape, "shape_type", None)
        # Flatten one level of groups — pictures/OLE inside a group still need crops
        shapes_to_check = []
        if stype == MSO_SHAPE_TYPE.GROUP:
            shapes_to_check = list(getattr(shape, "shapes", []))
        else:
            shapes_to_check = [shape]
        for s in shapes_to_check:
            st = getattr(s, "shape_type", None)
            if st is not None and int(st) in _RASTER_SHAPE_TYPES:
                left = getattr(s, "left", None)
                top = getattr(s, "top", None)
                width = getattr(s, "width", None)
                height = getattr(s, "height", None)
                if None not in (left, top, width, height):
                    regions.append({
                        "left": int(left), "top": int(top),
                        "width": int(width), "height": int(height),
                    })
    return regions


def _collect_shape_content(slide) -> tuple[str | None, str, list[str]]:
    """Extract (title, body_text, tables) from a slide's native XML, no LLM.

    Shapes are read in column-first order so that side-by-side blocks (e.g.
    three coloured columns) are transcribed block-by-block rather than row by
    row. Nav-bar shapes are skipped. Grouped shapes are flattened one level.
    Raster/OLE shapes carry no extractable text and are handled by the VLM tier.
    """
    title: str | None = None
    try:
        if slide.shapes.title is not None:
            title = (slide.shapes.title.text or "").strip() or None
    except Exception:  # noqa: BLE001
        title = None

    title_shape = None
    try:
        title_shape = slide.shapes.title
    except Exception:  # noqa: BLE001
        title_shape = None

    def _iter_shapes(shapes):
        from pptx.enum.shapes import MSO_SHAPE_TYPE  # noqa: PLC0415

        for shape in shapes:
            if _is_nav_shape(shape):
                continue
            if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.GROUP:
                yield from _iter_shapes(shape.shapes)
            else:
                yield shape

    text_blocks: list[str] = []
    tables: list[str] = []
    for shape in _column_aware_order(list(_iter_shapes(slide.shapes))):
        try:
            if getattr(shape, "has_table", False):
                md = _table_markdown(shape.table)
                if md:
                    tables.append(md)
                continue
            if shape is title_shape:
                continue
            if getattr(shape, "has_text_frame", False):
                md = _text_frame_markdown(shape.text_frame)
                if md:
                    text_blocks.append(md)
        except Exception:  # noqa: BLE001 — never let one bad shape kill the slide
            continue

    return title, "\n".join(text_blocks).strip(), tables


class PptxSlideRenderer:
    """Default renderer: LibreOffice → PDF → PyMuPDF PNGs + python-pptx metadata."""

    def __init__(self, *, dpi: int = _RENDER_DPI) -> None:
        self._dpi = dpi

    def render(self, pptx_bytes: bytes) -> list[RenderedSlide]:
        """Parse first; render slide images ONLY when the deck has visual content.

        - Deck with charts/pictures/SmartArt → LibreOffice→PNG, and EVERY slide
          carries its image so the LLM can verify/correct each slide against it.
        - Purely textual deck → no LibreOffice (fast, no crash surface); slides are
          text-only and the LLM restructures from the parser text alone.
        - LibreOffice failure on a visual deck → degrade to text-only, never lose
          the deck.
        """
        from pipeline.preprocessing.normalizers.pptx_converter import pptx_to_pdf_bytes  # noqa: PLC0415

        per_slide = self._pptx_stats(pptx_bytes)
        has_images = any(is_visual_from_stats(stats) for stats, *_ in per_slide)
        if not has_images:
            return self._slides_from(per_slide, images=None)

        pdf_bytes = pptx_to_pdf_bytes(pptx_bytes)
        if pdf_bytes is None:
            log.warning("render: LibreOffice unavailable — text-only extraction (no images)")
            return self._slides_from(per_slide, images=None)
        return self._slides_from(per_slide, images=self._pdf_to_pngs(pdf_bytes))

    @staticmethod
    def _slides_from(per_slide: list, images: list[bytes] | None) -> list[RenderedSlide]:
        """Build RenderedSlides from parser output, attaching an image per slide
        when ``images`` is provided. ``has_visual`` is True only for slides that
        genuinely have visual content (chart/picture/SmartArt) — decoupled from
        whether a PNG was rendered, so text-only slides in a visual deck are not
        routed to the VLM."""
        rendered: list[RenderedSlide] = []
        for idx, (stats, notes, title, text, tables) in enumerate(per_slide, start=1):
            png = images[idx - 1] if (images is not None and idx - 1 < len(images)) else b""
            rendered.append(
                RenderedSlide(
                    slide_number=idx,
                    image_png=png,
                    classification=classify_from_stats(stats),
                    notes=notes,
                    shape_stats=stats,
                    has_visual=is_visual_from_stats(stats),  # per-slide, not deck-level
                    needs_image=False,
                    parsed_title=title,
                    parsed_text=text,
                    parsed_tables=tables,
                )
            )
        return rendered

    def _pdf_to_pngs(self, pdf_bytes: bytes) -> list[bytes]:
        import fitz  # PyMuPDF  # noqa: PLC0415

        # LibreOffice-generated PDFs often carry a malformed logical structure
        # (tag) tree, so MuPDF floods stderr with "No common ancestor in
        # structure tree" once per page. That tree is only used for text
        # extraction / accessibility — it has NO effect on raster rendering via
        # get_pixmap — so silence the display to keep ingestion logs readable.
        try:
            fitz.TOOLS.mupdf_display_errors(False)
        except Exception:  # noqa: BLE001 — older/newer PyMuPDF without the toggle
            pass

        out: list[bytes] = []
        zoom = self._dpi / 72.0
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc:
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                out.append(pix.tobytes("png"))
        return out

    def _pptx_stats(
        self, pptx_bytes: bytes
    ) -> list[tuple[dict, str | None, str | None, str, list[str]]]:
        """Per-slide (stats, notes, title, body_text, tables) from python-pptx."""
        try:
            from pptx import Presentation  # noqa: PLC0415
        except ImportError:
            return []
        prs = Presentation(io.BytesIO(pptx_bytes))
        out: list[tuple[dict, str | None, str | None, str, list[str]]] = []
        for s in prs.slides:
            stats, notes = _collect_shape_stats(s)
            title, text, tables = _collect_shape_content(s)
            stats["visual_regions"] = _collect_visual_regions(s)
            stats["slide_width_emu"] = int(prs.slide_width)
            stats["slide_height_emu"] = int(prs.slide_height)
            out.append((stats, notes, title, text, tables))
        return out
