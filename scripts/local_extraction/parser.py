"""VLM reply parser — tolerates '### TITLE:' and bare 'TITLE:' equally.

Ported from compare_vlm_extraction.py with all fixes accumulated during
InternVL testing (filler phrases, page numbers, heading-hash title prefix,
TEXT: label omission fallback).
"""

from __future__ import annotations

import re

from pipeline.preprocessing.slides.backends import SlideContent

_LABEL_RE = re.compile(
    r"^(?:#{1,3}\s*)?(TITLE|TEXT|TABLE|VISUAL)\s*:",
    re.IGNORECASE | re.MULTILINE,
)
# Standalone page-number lines python-pptx picks up from slide-number text boxes
PAGE_NUM_RE = re.compile(r"^\s*-?\s*\d{1,3}\s*$", re.MULTILINE)

_FILLER_RE = re.compile(
    r"^\[(?:No visual (?:content|data)(?: described| provided| present)?|unclear|none)\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _dedup_lines(text: str, max_repeats: int = 3) -> str:
    """Remove consecutive duplicate lines — InternVL sometimes loops on complex slides."""
    lines = text.split("\n")
    out: list[str] = []
    run_line: str | None = None
    run_count = 0
    for line in lines:
        if line == run_line:
            run_count += 1
            if run_count <= max_repeats:
                out.append(line)
        else:
            run_line = line
            run_count = 1
            out.append(line)
    return "\n".join(out)


def _parse_vlm_reply(reply: str) -> SlideContent:
    if not reply:
        return SlideContent(title=None, text="", tables=[], visuals="")

    splits = list(_LABEL_RE.finditer(reply))
    sections: dict[str, str] = {}
    for i, m in enumerate(splits):
        label = m.group(1).upper()
        start = m.end()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(reply)
        sections[label] = reply[start:end].strip()

    title_raw = sections.get("TITLE", "").strip()
    title = title_raw if (title_raw and not title_raw.startswith("[")) else None
    text = sections.get("TEXT", "").strip()
    visuals = sections.get("VISUAL", "").strip()
    table_body = sections.get("TABLE", "").strip()
    tables = [table_body] if table_body else []

    # Fallback: model wrote TITLE: then body without ever writing TEXT:
    if not text and not visuals and title and "\n" in title:
        lines = title.split("\n", 1)
        title = lines[0].strip() or None
        text = lines[1].strip()

    text = _dedup_lines(_FILLER_RE.sub("", text)).strip()
    visuals = _dedup_lines(_FILLER_RE.sub("", visuals)).strip()

    # Pull title from first text line when model emitted none;
    # strip leading #+ hashes and "- " bullet markers
    if not title and text:
        first_line = text.split("\n")[0].strip()
        clean = re.sub(r"^#{1,6}\s*", "", first_line).strip()
        clean = re.sub(r"^-\s+", "", clean).strip()
        if clean:
            title = clean
            text = text[len(first_line):].strip()

    return SlideContent(title=title, text=text, tables=tables, visuals=visuals)
