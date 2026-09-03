"""Record-level summarization for multi-file Airtable records.

A single record (e.g. a D.Quals project) can carry several attachments across
multiple columns. Each file is extracted and summarized on its own; this module
synthesises ONE record-level summary from those per-file summaries, which the
embedding pipeline indexes as the parent document for the whole record.

Cost design (locked with user):
    • 0 files  → no summary.
    • 1 file   → reuse that file's own summary verbatim (NO Claude call).
    • 2+ files → one Haiku call over the per-file SUMMARIES (not full text).

Fail-safe: any error degrades to concatenating the per-file summaries so the
record-summary artifact is still produced and the per-file children still index.
"""

from __future__ import annotations

import logging
import re

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = logging.getLogger(__name__)

_DEFAULT_SUMMARY_MODEL = "claude-haiku-4-5-20251001"
_FALLBACK_SUMMARY_CHARS = 1800  # head of body used when a file has no DOCUMENT_SUMMARY

_claude_retry = retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=60),
    retry=retry_if_exception_type((
        anthropic.RateLimitError,
        anthropic.InternalServerError,
        anthropic.APIConnectionError,
        anthropic.APITimeoutError,
    )),
)

_DOCUMENT_SUMMARY_RE = re.compile(
    r"^DOCUMENT_SUMMARY:\s*(.+?)(?=\n\s*\n|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_FRONT_MATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)

_RECORD_SUMMARY_PROMPT = """\
You are writing ONE rich, descriptive, self-contained record-level summary for a
consulting project documented by SEVERAL files (proposals, decks, deliverables, reports).
Below are the individual summaries of each file. Synthesise them into a single
information-complete summary of the WHOLE project — capture the combined substance of
every file, not a thin abstract. It is embedded for semantic search AND used as the
parent context for the project's slide/section chunks.

Begin with ONE sentence naming exactly what the project is (used on its own as a chunk
prefix, so it must stand completely alone and be specific). Then write a thorough
multi-sentence summary (aim for 8-12 sentences, more if the files are rich) covering,
across ALL files wherever present:
- the client / organisation and any partners, funders or stakeholders;
- the sector(s) and sub-themes;
- the geography (regions, countries, markets);
- the time period and key dates;
- the mandate / objective and the questions the work answered;
- the approach / methodology and scope of work;
- the key findings and the evidence/metrics behind them (name concrete numbers,
  percentages and named programmes/instruments);
- the recommendations, decisions and the outcomes or impact;
- the distinct contribution of each file where they differ (e.g. proposal vs final report).
Name concrete entities throughout so the summary matches questions like "have we worked
on <topic> in <country> for <client>?" and "what did we recommend on <theme>?".

Be comprehensive but faithful: use ONLY the content provided and never invent facts,
numbers or entities. Output flowing prose only — no headings, no bullet points, no
preamble.

Per-file summaries:
{digest}
"""


def extract_file_summary(normalized_text: str) -> str:
    """Best-effort per-file summary from a normalized.txt artifact.

    Prefers the ``DOCUMENT_SUMMARY:`` block the slides normalizer emits; otherwise
    falls back to the head of the body (after any ``--- ... ---`` metadata header).
    """
    if not normalized_text:
        return ""
    m = _DOCUMENT_SUMMARY_RE.search(normalized_text)
    if m:
        return " ".join(m.group(1).split())
    body = _FRONT_MATTER_RE.sub("", normalized_text, count=1).strip()
    head = body[:_FALLBACK_SUMMARY_CHARS].strip()
    return " ".join(head.split())


def usable_sections(sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop empty per-file summaries (order-preserving)."""
    return [(label, text.strip()) for label, text in sections if text.strip()]


def build_record_summary_content(usable: list[tuple[str, str]]) -> list[dict]:
    """Message ``content`` for the record-level summary call (2+ files).

    Single source of truth shared by the sync ``RecordSummarizer`` and the batch
    orchestrator. Callers must short-circuit the 0- and 1-file cases first.
    """
    digest = "\n\n".join(f"## {label}\n{text}" for label, text in usable)
    return [{"type": "text", "text": _RECORD_SUMMARY_PROMPT.format(digest=digest)}]


class RecordSummarizer:
    """Synthesise a record-level summary from per-file summaries (Haiku, 1 call)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_SUMMARY_MODEL,
        max_tokens: int = 2048,
    ) -> None:
        import anthropic  # noqa: PLC0415

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def summarize(self, sections: list[tuple[str, str]]) -> str | None:
        """Return a combined summary for ``[(label, file_summary), ...]``.

        ``label`` is a human-readable file/column tag. Single-file records should
        NOT reach here (the caller reuses the lone file summary directly). Returns
        None only when there is no usable input.
        """
        usable = usable_sections(sections)
        if not usable:
            return None
        if len(usable) == 1:
            # Defensive: caller normally short-circuits this; no need to spend a call.
            return usable[0][1]

        content = build_record_summary_content(usable)
        try:
            text = self._call(content)
            return text or self._fallback(usable)
        except Exception:  # noqa: BLE001 — never fail the record over the summary
            log.warning("record_summary: Claude call failed, concatenating", exc_info=True)
            return self._fallback(usable)

    @_claude_retry
    def _call(self, content: list[dict]) -> str:
        """One Haiku call, retried on transient 429/5xx so high concurrency is safe."""
        with self._client.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": content}],
        ) as stream:
            msg = stream.get_final_message()
        blocks = getattr(msg, "content", None) or []
        return "".join(
            getattr(b, "text", "") or ""
            for b in blocks
            if getattr(b, "type", None) == "text"
        ).strip()

    @staticmethod
    def _fallback(usable: list[tuple[str, str]]) -> str:
        return " ".join(text for _, text in usable)
