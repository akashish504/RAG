"""Source-agnostic NL planner.

Refactored from :mod:`pipeline.api.nl_airtable_query`. The original was
hardcoded to one Airtable table; this version takes a
:class:`retrieval.models.SchemaDescriptor` and produces a richer plan that
can drive ``semantic_search``, ``airtable_lookup``, or both via ``hybrid``.
"""

from __future__ import annotations

import json
import re
from json import JSONDecoder
from typing import Any

import anthropic
import structlog

from retrieval.models import SchemaDescriptor, SearchMode
from retrieval.planner.anthropic_settings import get_anthropic_query_settings
from retrieval.planner.prompts import (
    PLANNER_SYSTEM,
    build_planner_user_message,
)
from retrieval.planner.query_classifier import classification_to_plan, classify_question

log = structlog.get_logger(__name__)

_BRACED_FIELD = re.compile(r"\{([^}]+)\}")
_VALID_MODES: tuple[SearchMode, ...] = ("airtable_only", "semantic_only", "hybrid")


# ---------------------------------------------------------------------------
# JSON extraction (tolerant of fences and chatter)
# ---------------------------------------------------------------------------


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty planner response from Claude.")
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    decoder = JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and (
            "mode" in obj or "airtable_formula" in obj or "semantic_query" in obj
        ):
            return obj
    obj = json.loads(text)
    if isinstance(obj, dict):
        return obj
    raise ValueError("Planner output is not a JSON object.")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_formula_fields(formula: str, allowed: set[str]) -> None:
    for raw in _BRACED_FIELD.findall(formula or ""):
        name = raw.strip()
        if name not in allowed:
            raise ValueError(
                f"Planner referenced unknown field {name!r}. "
                f"Allowed examples: {sorted(allowed)[:16]}"
            )


def _normalise_int(value: Any, *, default: int | None) -> int | None:
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("", "all", "none", "null", "unlimited"):
            return None
        try:
            value = int(s)
        except ValueError:
            try:
                value = int(float(s))
            except ValueError:
                return default
    if isinstance(value, float):
        value = int(value)
    if not isinstance(value, int):
        return default
    if value <= 0:
        return None
    return value


def _normalise_mode(value: Any, fallback: SearchMode) -> SearchMode:
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _VALID_MODES:
            return v  # type: ignore[return-value]
    return fallback


# ---------------------------------------------------------------------------
# Plan dataclass + planner entry point
# ---------------------------------------------------------------------------


def plan_query(
    *,
    question: str,
    schema: SchemaDescriptor,
    default_mode: SearchMode = "hybrid",
    default_top_k: int = 10,
) -> dict[str, Any]:
    """Call Claude to produce a retrieval plan; does not execute anything."""

    if not question.strip():
        raise ValueError("question must be non-empty")

    classified = classify_question(question=question, schema=schema)
    if classified is not None:
        return classification_to_plan(classified)

    user_text = build_planner_user_message(question=question, schema=schema)
    settings = get_anthropic_query_settings()
    client = anthropic.Anthropic(api_key=settings.api_key)
    msg = client.messages.create(
        model=settings.model,
        max_tokens=settings.max_output_tokens,
        system=PLANNER_SYSTEM,
        messages=[{"role": "user", "content": user_text}],
    )
    parts: list[str] = []
    for block in msg.content:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            parts.append(block.text)
    raw = "".join(parts).strip()
    if not raw:
        raise RuntimeError("Empty planner response from Claude.")

    plan_raw = _extract_json_object(raw)
    formula = plan_raw.get("airtable_formula") or ""
    if not isinstance(formula, str):
        raise ValueError('"airtable_formula" must be a string')
    formula = formula.strip()

    semantic_query = plan_raw.get("semantic_query") or ""
    if not isinstance(semantic_query, str):
        raise ValueError('"semantic_query" must be a string')
    semantic_query = semantic_query.strip() or question.strip()

    mode = _normalise_mode(plan_raw.get("mode"), default_mode)
    if mode == "airtable_only" and "structured" not in schema.capabilities:
        mode = "semantic_only"
    if mode == "semantic_only" and "semantic" not in schema.capabilities:
        mode = "airtable_only"

    if formula:
        _validate_formula_fields(formula, set(schema.field_names()))

    # Respect Claude's explicit uncertain flag; never derive it from mode+formula
    # because hybrid with no formula is a valid deliberate choice (expertise query
    # with no useful structured pre-filter), not a sign of confusion.
    uncertain = bool(plan_raw.get("uncertain", False))

    query_kind: str
    if uncertain:
        query_kind = "uncertain"
    elif mode == "airtable_only":
        query_kind = "structured"
    elif mode == "semantic_only":
        query_kind = "semantic"
    else:
        query_kind = "hybrid"

    return {
        "mode": mode,
        "airtable_formula": formula,
        "semantic_query": semantic_query,
        "max_records": _normalise_int(plan_raw.get("max_records"), default=None),
        "top_k": _normalise_int(plan_raw.get("top_k"), default=default_top_k) or default_top_k,
        "rationale": str(plan_raw.get("rationale") or "").strip(),
        "model": settings.model,
        "lookup_fields": None,
        "uncertain": uncertain,
        "query_kind": query_kind,
    }
