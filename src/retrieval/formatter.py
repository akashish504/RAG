
"""Response formatter — classifies shape, builds markdown table, optionally
synthesises a Claude answer.

Lifted and merged from :mod:`pipeline.api.nl_response_shape` and
:mod:`pipeline.api.nl_query_human_response`. Operates on the unified
:class:`retrieval.models.SearchResult` shape so a single formatter handles
hits from Airtable rows AND OpenSearch chunks.
"""

from __future__ import annotations

import json
import re
from json import JSONDecoder
from typing import Any

import anthropic
import structlog

from retrieval.citations import (
    append_references_to_answer,
    assign_cite_ids_to_hits,
    build_references_from_hits,
)
from retrieval.models import (
    Hint,
    ResponseMode,
    SearchResponse,
    SearchResult,
)
from retrieval.planner.anthropic_settings import get_anthropic_query_settings

log = structlog.get_logger(__name__)


_DEFAULT_TABLE_COLUMNS = [
    "Display Name",
    "Email",
    "Job Title",
    "Office Location",
    "Business",
    "Office Region",
]
_FIELD_IN_FORMULA = re.compile(r"\{([^}]+)\}")


# ---------------------------------------------------------------------------
# Shape classifier (Claude with rule-based fallback)
# ---------------------------------------------------------------------------

_SHAPE_SYSTEM = """You classify how to present search results to the user.

Output a SINGLE JSON object only, no markdown, no extra text.

Allowed response_mode values:
- "count_only" — user only wants a quantity.
- "column_subset" — user wants specific attributes listed (names, emails, titles,
  ...) but NOT every field.
- "full_records" — user wants rich / full rows or several unrelated columns.

For "column_subset", set "columns" to an array of field names chosen ONLY from
allowed_field_names. Use the smallest set that answers the question.
For "count_only" / "full_records", set "columns" to [].

Required JSON shape:
{"response_mode": "<count_only|column_subset|full_records>", "columns": ["..."]}"""


def _parse_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty shape response")
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    decoder = JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
            if isinstance(obj, dict) and "response_mode" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    obj = json.loads(text)
    if isinstance(obj, dict):
        return obj
    raise ValueError("unparseable shape JSON")


def _fallback_shape(question: str) -> dict[str, Any]:
    q = (question or "").lower()
    if any(p in q for p in (
        "how many", "how much", "count ", "number of",
        "total number", "what is the total", "size of",
    )):
        return {"response_mode": "count_only", "columns": []}
    if ("name" in q or "names" in q) and any(
        p in q for p in ("list", "all ", "give me", "show me", "every")
    ):
        return {"response_mode": "column_subset", "columns": ["Display Name"]}
    return {"response_mode": "full_records", "columns": []}


def classify_response_shape(
    *,
    question: str,
    allowed_field_names: list[str],
    row_count: int,
) -> dict[str, Any]:
    allowed = set(allowed_field_names)
    payload = {
        "user_question": question.strip(),
        "matching_row_count": row_count,
        "allowed_field_names": allowed_field_names,
    }
    settings = get_anthropic_query_settings()
    try:
        client = anthropic.Anthropic(api_key=settings.api_key)
        msg = client.messages.create(
            model=settings.model,
            max_tokens=512,
            system=_SHAPE_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        )
        parts: list[str] = []
        for block in msg.content:
            if getattr(block, "type", None) == "text" and getattr(block, "text", None):
                parts.append(block.text)
        data = _parse_json_object("".join(parts).strip())
    except Exception as exc:  # noqa: BLE001
        log.warning("shape_classify_failed", error=str(exc))
        data = _fallback_shape(question)

    mode = str(data.get("response_mode") or "full_records").strip()
    if mode not in {"count_only", "column_subset", "full_records"}:
        mode = _fallback_shape(question)["response_mode"]

    cols_raw = data.get("columns") or []
    if not isinstance(cols_raw, list):
        cols_raw = []
    columns = [str(c) for c in cols_raw if isinstance(c, str) and c in allowed]
    if mode == "column_subset" and not columns:
        fb = _fallback_shape(question)
        if fb["response_mode"] == "column_subset":
            columns = [c for c in fb.get("columns", []) if c in allowed]
        if not columns and "Display Name" in allowed:
            columns = ["Display Name"]
        elif not columns:
            mode = "full_records"
    return {"response_mode": mode, "columns": columns}


# ---------------------------------------------------------------------------
# Markdown table builder (works on SearchResult.payload["fields"] when present)
# ---------------------------------------------------------------------------


def _flatten_cell(value: Any, *, max_len: int = 200) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        s = value.replace("\n", " ").replace("|", "\\|")
    elif isinstance(value, (int, float, bool)):
        s = str(value)
    elif isinstance(value, list):
        if not value:
            return ""
        first = value[0]
        if isinstance(first, dict) and "filename" in first:
            names = [str(x.get("filename", "")) for x in value if isinstance(x, dict)]
            s = "; ".join(names[:3]) + (f" (+{len(value) - 3} more)" if len(value) > 3 else "")
        else:
            s = ", ".join(str(x) for x in value[:12])
            if len(value) > 12:
                s += f" … (+{len(value) - 12})"
        s = s.replace("|", "\\|")
    elif isinstance(value, dict):
        s = json.dumps(value, default=str)[:max_len]
    else:
        s = str(value)
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


def _row_fields(hit: SearchResult) -> dict[str, Any]:
    fields = hit.payload.get("fields") if isinstance(hit.payload, dict) else None
    if isinstance(fields, dict):
        return fields
    return hit.metadata or {}


def _infer_columns(
    formula: str,
    hits: list[SearchResult],
    *,
    max_cols: int = 8,
) -> list[str]:
    keys: set[str] = set()
    for h in hits[:50]:
        keys.update(_row_fields(h).keys())

    ordered: list[str] = []
    for name in _FIELD_IN_FORMULA.findall(formula or ""):
        clean = name.strip()
        if clean in keys and clean not in ordered:
            ordered.append(clean)
    for name in _DEFAULT_TABLE_COLUMNS:
        if name in keys and name not in ordered:
            ordered.append(name)
            if len(ordered) >= max_cols:
                break
    for name in sorted(keys):
        if name not in ordered:
            ordered.append(name)
            if len(ordered) >= max_cols:
                break
    return ordered[:max_cols]


def build_markdown_table(
    hits: list[SearchResult],
    *,
    formula: str = "",
    columns: list[str] | None = None,
    max_rows: int = 50,
) -> tuple[str, bool, list[str]]:
    """Return ``(markdown, truncated, columns_used)``."""

    if not hits:
        return "_No matching rows._", False, []
    cols = columns or _infer_columns(formula, hits)
    if not cols:
        return f"_Retrieved **{len(hits)}** hit(s); no field keys to tabulate._", False, []
    truncated = len(hits) > max_rows
    shown = hits[:max_rows]
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    lines = [header, sep]
    for h in shown:
        fields = _row_fields(h)
        cells = [_flatten_cell(fields.get(c)) for c in cols]
        lines.append("| " + " | ".join(cells) + " |")
    md = "\n".join(lines)
    if truncated:
        md += f"\n\n_Showing first {max_rows} of **{len(hits)}** rows._"
    return md, truncated, cols


# ---------------------------------------------------------------------------
# Claude answer synthesis (optional second pass)
# ---------------------------------------------------------------------------


def _compact_hit_for_answer(h: SearchResult) -> dict[str, Any]:
    """Slim hit shape sent to the synthesis Claude call.

    Only the fields the model needs to write a grounded answer are included.
    Raw text is capped at 400 chars — enough for factual grounding without
    blowing up the synthesis context window.
    """
    return {
        "cite_id": h.citations[0].cite_id if h.citations else None,
        "source": h.source,
        "score": round(h.score, 4),
        "text": (h.text or "")[:400],
        "name": h.metadata.get("Display Name") or h.metadata.get("primary_key"),
        "email": h.metadata.get("Email") or h.metadata.get("primary_key"),
        "section": h.metadata.get("section_canonical"),
        "citation_url": h.citation_url,
        "citations": [c.to_dict() for c in h.citations],
    }


def synthesize_answer(
    *,
    question: str,
    hits: list[SearchResult],
    plan: dict[str, Any] | None,
    max_sample: int = 15,
) -> str:
    settings = get_anthropic_query_settings()
    sample = [_compact_hit_for_answer(h) for h in hits[:max_sample]]
    payload = {
        "user_question": question,
        "matching_row_count": len(hits),
        "plan": plan or {},
        "hits_sample": sample,
        "note": (
            "matching_row_count is the total. Use ONLY information in hits_sample. "
            "Do not invent people, titles, emails, or facts not present. "
            "Never cite or link to S3 paths. "
            "Every hit with a non-null citation_url or citations[].url is an Airtable "
            "profile — you MUST include that link when you mention that person or "
            "their data."
        ),
    }
    system = (
        "You are a helpful Dalberg knowledge analyst. Answer the user's question "
        "in clear natural language using ONLY the provided hits_sample. "
        "Do NOT invent people, titles, emails, languages, locations, or any fact "
        "not explicitly present in hits_sample.\n\n"
        "FORMAT:\n"
        "- Lead with the count if the question asks for a count.\n"
        "- Use a concise bullet list (one bullet per person) when listing people; "
        "include Job Title and Office Location when present in the hit metadata.\n"
        "- Keep prose answers to one paragraph or a short bullet list. No JSON.\n"
        "- Note when matching_row_count > number of hits shown, so the user knows "
        "the list may be incomplete.\n\n"
        "CITATIONS — required whenever you name a person or quote profile content:\n"
        "- Append a link immediately after the person's name:\n"
        "  Jane Doe ([profile](https://airtable.com/appXXX/tblXXX/recXXX))\n"
        "  Use citation_url or citations[0].url from the hit data — never construct "
        "a URL yourself. If citation_url is null, name the person without a link.\n"
        "- Use inline numeric refs [1], [2] when the hit has a cite_id.\n"
        "- End with a '## References' section only when you named one or more people:\n"
        "  [1] Jane Doe — https://airtable.com/appXXX/tblXXX/recXXX\n"
        "  [2] John Smith — https://airtable.com/appXXX/tblXXX/recXXX\n"
        "- Never use s3://, internal chunk IDs, or non-Airtable URLs."
    )
    client = anthropic.Anthropic(api_key=settings.api_key)
    msg = client.messages.create(
        model=settings.model,
        max_tokens=min(2048, settings.max_output_tokens),
        system=system,
        messages=[{"role": "user", "content": json.dumps(payload, indent=2, default=str)}],
    )
    parts: list[str] = []
    for block in msg.content:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            parts.append(block.text)
    return "".join(parts).strip() or "No summary could be generated."


# ---------------------------------------------------------------------------
# Top-level formatter used by the ``search`` MCP tool
# ---------------------------------------------------------------------------


def format_response(
    *,
    question: str,
    hits: list[SearchResult],
    hints: list[Hint],
    plan: dict[str, Any] | None,
    allowed_field_names: list[str],
    diagnostics: dict[str, Any],
    include_llm_answer: bool,
    formula_for_columns: str = "",
) -> SearchResponse:
    """Bundle hits + classifier + markdown + (optional) LLM answer into the
    uniform :class:`SearchResponse` envelope returned by every MCP tool.
    """

    n = len(hits)
    assign_cite_ids_to_hits(hits)
    references = build_references_from_hits(hits)

    if include_llm_answer:
        shape = classify_response_shape(
            question=question,
            allowed_field_names=allowed_field_names,
            row_count=n,
        )
    else:
        shape = {"response_mode": "full_records", "columns": []}
    mode = ResponseMode(shape["response_mode"])
    columns = list(shape["columns"])

    # Synthesize the answer FIRST when requested, so we can skip building
    # the redundant markdown_table whenever a non-empty answer is produced.
    # The synthesis pass reads in-memory hits (not the wire payload), so
    # answer quality is unaffected by any wire-shape changes.
    answer_text: str | None = None
    synth_succeeded = False
    if include_llm_answer:
        synth_raw = ""
        try:
            synth_raw = synthesize_answer(question=question, hits=hits, plan=plan)
        except Exception as exc:  # noqa: BLE001
            log.warning("answer_synth_failed", error=str(exc))
            synth_raw = ""
        # `append_references_to_answer` adds a non-empty References block
        # even when the synthesised body is empty — check the raw body to
        # detect synthesis success/failure, not the post-append result.
        synth_succeeded = bool(synth_raw.strip())
        answer_text = append_references_to_answer(synth_raw, references)

    if mode is ResponseMode.COUNT_ONLY:
        markdown_table = ""
        if not include_llm_answer:
            answer_text = str(n)
    elif synth_succeeded:
        # Answer present — drop the redundant markdown table to keep the
        # payload small. Synthesis-failure path falls through to else.
        markdown_table = ""
    elif mode is ResponseMode.COLUMN_SUBSET:
        markdown_table, _, columns = build_markdown_table(
            hits, formula=formula_for_columns, columns=columns
        )
    else:
        markdown_table, _, columns_used = build_markdown_table(
            hits, formula=formula_for_columns
        )
        if not columns:
            columns = columns_used

    return SearchResponse(
        ok=True,
        hits=hits,
        hints=hints,
        references=references,
        response_mode=mode,
        columns=columns,
        markdown_table=markdown_table,
        answer=answer_text,
        plan=plan,
        diagnostics=diagnostics,
    )
