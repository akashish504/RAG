"""Rule-based query classification for structured skill/interest/language lookups.

Literal keyword questions ("who plays guitar", "who speaks French") are routed to
``airtable_only`` with ``FIND("keyword", LOWER({Field}))`` formulas against
schema fields such as Skills, Interests, Languages, and Hobbies.

Expertise / experience questions defer to the LLM planner (semantic or hybrid).
Mixed questions combine a structured formula with semantic search.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from retrieval.models import SchemaDescriptor

QueryKind = Literal["literal_keyword", "expertise", "mixed", "uncertain"]

# Categories → candidate Airtable field name stems (matched case-insensitively).
_FIELD_CATEGORY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "skills": ("skills", "skill"),
    "interests": ("interests", "interest"),
    "languages": ("languages", "language"),
    "hobbies": ("hobbies", "hobby"),
}

# Display / profile fields to request alongside structured matches.
_DISPLAY_FIELD_CANDIDATES: tuple[str, ...] = (
    "Display Name",
    "Job Title",
    "Office Location",
    "Email",
)

# Always include in enrichment / lookup field lists when present in schema.
_PRIORITY_STRUCTURED_FIELDS: tuple[str, ...] = (
    "Skills",
    "Interests",
    "Languages",
)

_EXPERTISE_SIGNALS = re.compile(
    r"\b("
    r"worked\s+on|work\s+on|experience\s+(?:in|with)|experienced\s+in|"
    r"expert(?:ise)?\s+(?:in|on|with)|background\s+in|"
    r"project\s+experience|sector\s+experience|consulting\s+experience|"
    r"years?\s+of\s+experience|private\s+sector|public\s+sector|"
    r"find\s+(?:an?\s+)?(?:expert|consultant|someone\s+with)"
    r")\b",
    re.IGNORECASE,
)

# (pattern, field categories to search, optional keyword cleanup)
_LITERAL_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\bplays?\s+(?:the\s+)?(.+?)(?:\?|$)", re.IGNORECASE), ("skills",)),
    (re.compile(r"\bspeaks?\s+(?:fluent\s+)?(.+?)(?:\?|$)", re.IGNORECASE), ("languages",)),
    (re.compile(r"\bdoes\s+(.+?)(?:\?|$)", re.IGNORECASE), ("skills", "interests", "hobbies")),
    (re.compile(r"\bknows?\s+(?:how\s+to\s+)?(.+?)(?:\?|$)", re.IGNORECASE), ("skills", "languages")),
    (re.compile(r"\binterested\s+in\s+(.+?)(?:\?|$)", re.IGNORECASE), ("interests", "hobbies")),
    (re.compile(r"\bhobbies?\s+(?:include|is|are)\s+(.+?)(?:\?|$)", re.IGNORECASE), ("hobbies", "interests")),
    (re.compile(r"\bwith\s+(?:skill|skills)\s+in\s+(.+?)(?:\?|$)", re.IGNORECASE), ("skills",)),
)


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    kind: QueryKind
    keyword: str | None
    target_fields: tuple[str, ...]
    mode: Literal["airtable_only", "semantic_only", "hybrid"]
    airtable_formula: str
    semantic_query: str
    lookup_fields: tuple[str, ...]
    rationale: str
    uncertain: bool = False


def _schema_field_map(schema: SchemaDescriptor) -> dict[str, str]:
    """Lowercase field name → canonical field name."""

    return {f.name.lower(): f.name for f in schema.fields}


def _resolve_category_fields(
    categories: tuple[str, ...],
    field_map: dict[str, str],
) -> list[str]:
    resolved: list[str] = []
    for category in categories:
        for candidate in _FIELD_CATEGORY_CANDIDATES.get(category, ()):
            name = field_map.get(candidate)
            if name and name not in resolved:
                resolved.append(name)
                break
    return resolved


def _clean_keyword(raw: str) -> str:
    keyword = raw.strip().strip("?.!,\"'")
    # Drop trailing clauses ("who also …") AND trailing location/scope phrases
    # ("piano at dalberg" → "piano", "french in the nairobi office" → "french")
    # so the FIND() term is just the literal value, not the whole tail.
    keyword = re.split(
        r"\b(?:who|and|with|that|at|in|for|from|within|across)\b",
        keyword,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return keyword.strip().strip("?.!,\"'")


def _extract_literal_keyword(question: str) -> tuple[str, tuple[str, ...]] | None:
    for pattern, categories in _LITERAL_PATTERNS:
        match = pattern.search(question)
        if not match:
            continue
        keyword = _clean_keyword(match.group(1))
        if keyword and len(keyword) >= 2:
            return keyword, categories
    return None


def _find_formula(keyword: str, field_names: list[str]) -> str:
    escaped = keyword.replace('"', '\\"')
    clauses = [f'FIND("{escaped}", LOWER({{{name}}}))' for name in field_names]
    if len(clauses) == 1:
        return clauses[0]
    return f"OR({', '.join(clauses)})"


def lookup_fields_for_schema(
    schema: SchemaDescriptor,
    *,
    matched_fields: list[str] | None = None,
) -> list[str]:
    """Fields to return for structured lookups and semantic enrichment."""

    names = {f.name for f in schema.fields}
    out: list[str] = []
    for candidate in _DISPLAY_FIELD_CANDIDATES:
        if candidate in names and candidate not in out:
            out.append(candidate)
    for field in matched_fields or ():
        if field in names and field not in out:
            out.append(field)
    for priority in _PRIORITY_STRUCTURED_FIELDS:
        if priority in names and priority not in out:
            out.append(priority)
    return out


def classify_question(
    *,
    question: str,
    schema: SchemaDescriptor,
) -> ClassificationResult | None:
    """Return a deterministic plan for literal/mixed queries, or None to use LLM."""

    if "structured" not in schema.capabilities:
        return None

    q = question.strip()
    if not q:
        return None

    field_map = _schema_field_map(schema)
    structured = {
        cat: name
        for cat, candidates in _FIELD_CATEGORY_CANDIDATES.items()
        for c in candidates
        if (name := field_map.get(c))
    }
    if not structured:
        return None

    literal = _extract_literal_keyword(q)
    has_expertise = bool(_EXPERTISE_SIGNALS.search(q))

    if not literal:
        return None

    keyword, categories = literal
    target_fields = _resolve_category_fields(categories, field_map)
    if not target_fields:
        return None

    formula = _find_formula(keyword, target_fields)
    lookup_fields = tuple(lookup_fields_for_schema(schema, matched_fields=target_fields))

    if has_expertise:
        return ClassificationResult(
            kind="mixed",
            keyword=keyword,
            target_fields=tuple(target_fields),
            mode="hybrid",
            airtable_formula=formula,
            semantic_query=q,
            lookup_fields=lookup_fields,
            rationale=(
                f"Mixed query: structured {target_fields} lookup for {keyword!r} "
                f"plus semantic search for experience/expertise context."
            ),
            uncertain=False,
        )

    field_label = target_fields[0] if len(target_fields) == 1 else "/".join(target_fields)
    return ClassificationResult(
        kind="literal_keyword",
        keyword=keyword,
        target_fields=tuple(target_fields),
        mode="airtable_only",
        airtable_formula=formula,
        semantic_query="",
        lookup_fields=lookup_fields,
        rationale=(
            f"Literal keyword lookup: search {field_label} for {keyword!r} "
            f"using FIND() — structured fields hold this data, not CV chunk headers."
        ),
        uncertain=False,
    )


def classification_to_plan(result: ClassificationResult) -> dict[str, Any]:
    """Convert a :class:`ClassificationResult` to the plan_query return shape."""

    return {
        "mode": result.mode,
        "airtable_formula": result.airtable_formula,
        "semantic_query": result.semantic_query or "",
        "max_records": None,
        "top_k": 10,
        "rationale": result.rationale,
        "query_kind": result.kind,
        "lookup_fields": list(result.lookup_fields),
        "uncertain": result.uncertain,
        "model": "rule-based",
    }
