"""HTTP client for vLLM's OpenAI-compatible /v1/chat/completions endpoint.

One instance shared across all worker threads — urllib.request is thread-safe.
"""

from __future__ import annotations

import base64
import json
import urllib.request

from pipeline.preprocessing.slides.backends import SlideContent

from .parser import _parse_vlm_reply

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

# Tight summary prompts for local VLMs with 8K context.
# The richer Claude-style prompts cause InternVL to loop — these demand
# exactly 3-5 sentences and stop cleanly.
_DECK_SUMMARY_PROMPT = """\
Write a 3-5 sentence summary of this presentation. Cover: what the document is, \
who it is for, the main topics, and any key facts or numbers. Be specific and factual. \
Do not repeat yourself. Output only the summary, nothing else.

{digest}
"""

_RECORD_SUMMARY_PROMPT = """\
Write a 3-5 sentence summary of this consulting project based on the per-file summaries \
below. Cover: what the project is, who it is for, the main findings or deliverables, and \
any key facts or numbers. Be specific and factual. Do not repeat yourself. \
Output only the summary, nothing else.

{digest}
"""

# Max tokens for summary calls — hard cap so InternVL doesn't loop.
SUMMARY_MAX_TOKENS = 512

# InternVL has 8K context; cap digests to leave room for prompt overhead (~400 tokens).
DECK_DIGEST_MAX_CHARS = 3000
RECORD_DIGEST_MAX_CHARS = 4000


class VLMClient:
    """Thread-safe vLLM HTTP client. One instance, many threads."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://localhost:8000",
        max_tokens: int = 2048,
        timeout_s: int = 300,
    ) -> None:
        self._model = model
        self._base = base_url.rstrip("/")
        self._max_tokens = max_tokens
        self._timeout = timeout_s

    def _chat(self, messages: list[dict], *, max_tokens: int | None = None) -> str:
        body = json.dumps({
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "temperature": 0,
        }).encode()
        req = urllib.request.Request(
            f"{self._base}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()

    def _img_block(self, png: bytes) -> dict:
        b64 = base64.standard_b64encode(png).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}

    def extract_slide(self, png: bytes) -> SlideContent:
        """Vision call: one slide PNG → structured SlideContent."""
        reply = self._chat([{"role": "user", "content": [
            self._img_block(png),
            {"type": "text", "text": _VISUAL_SLIDE_PROMPT},
        ]}])
        return _parse_vlm_reply(reply)

    def summarize_deck(self, digest: str) -> str:
        """Text-only call: slide digest → DOCUMENT_SUMMARY paragraph."""
        return self._chat(
            [{"role": "user", "content": [
                {"type": "text", "text": _DECK_SUMMARY_PROMPT.format(digest=digest)},
            ]}],
            max_tokens=SUMMARY_MAX_TOKENS,
        )

    def summarize_record(self, digest: str) -> str:
        """Text-only call: per-file summaries → record-level paragraph."""
        return self._chat(
            [{"role": "user", "content": [
                {"type": "text", "text": _RECORD_SUMMARY_PROMPT.format(digest=digest)},
            ]}],
            max_tokens=SUMMARY_MAX_TOKENS,
        )
