"""Vocabulary-grounded facet extraction for natural-language queries.

Turns a free-text query into structured facet filters by matching it against the
**actual** facet values present in the index (fetched via a terms aggregation and
cached). Grounding in real values means a derived filter can never reference a
value that isn't in the data — so filtered-KNN never silently returns nothing
because of a hallucinated/misspelled facet value.

Example: "financial inclusion work in East Africa for the Gates Foundation"
→ {"project_region": ["East Africa"], "client_organisation": ["Gates Foundation"]}
(only the values that actually exist as keywords in mcp-d-quals match).
"""

from __future__ import annotations

import re
import time
import unicodedata
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from opensearchpy import OpenSearch

log = structlog.get_logger(__name__)

# Categorical facets worth matching against the query text. Dates and the
# confidential boolean flags are intentionally excluded (not substring-matchable).
MATCHABLE_FACETS: tuple[str, ...] = (
    # D.Quals
    "client_organisation",
    "practice_area",
    "project_region",
    "project_location",
    "dalberg_entity",
    "insight_type",
    # Knowledge Library (union; per-index vocab means only present values ever match)
    "kd_type",
    "country_region",
    "author",
    "team",
    "item_type",
    "client",
    "language",
    # Proposal Library (union; per-index vocab means only present values ever match)
    "country",
    "region",
    "project_type",
)

_VOCAB_TTL_S = 300.0       # re-fetch the facet vocabulary at most every 5 min
_MAX_TERMS_PER_FACET = 1000

# Facets that are FUZZY, human-assigned taxonomies. A query term matching one of
# these is a SOFT signal about intent, NOT a hard constraint: the same project
# can legitimately span "Talent & Leadership" and "Strategy", so applying the
# matched value as a hard filter silently drops correct off-tag records. These
# are therefore excluded from the hard filter set (see :func:`split_soft`). The
# remaining matchable facets (client, region, location, dates, …) are genuine
# categorical constraints and stay hard.
SOFT_FACETS: frozenset[str] = frozenset({"practice_area", "insight_type"})


def split_soft(
    derived: dict[str, list[str]],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Partition derived facets into ``(hard, soft)`` by :data:`SOFT_FACETS`.

    ``hard`` facets are applied as ``bool.filter`` (exact constraints); ``soft``
    facets are NOT filtered in v1 (they would gate out valid off-tag records) —
    recall is left to KNN + BM25 + reranker. A future tie-breaker may re-use the
    ``soft`` partition to nudge on-tag hits without excluding off-tag ones.
    """
    hard: dict[str, list[str]] = {}
    soft: dict[str, list[str]] = {}
    for field, values in derived.items():
        (soft if field in SOFT_FACETS else hard)[field] = values
    return hard, soft


# Curated, HIGH-CONFIDENCE term → (facet_field, canonical_value) aliases. Applied
# as an extra facet filter when the term is word-present in the query. Kept
# deliberately tiny: only unambiguous equivalences safe to apply as a hard AND
# filter. Broad/ambiguous term→field mapping (PD, PA, "health", …) lives in the
# planner prompt glossary, where the LLM can ALSO search descriptions rather than
# hard-filter and risk over-narrowing. Extend conservatively.
_FACET_VALUE_ALIASES: dict[str, tuple[str, str]] = {
    "d.capital": ("dalberg_entity", "D. Capital"),
    "d capital": ("dalberg_entity", "D. Capital"),
    "dcapital": ("dalberg_entity", "D. Capital"),
}

_WS_RE = re.compile(r"\s+")
_AMP_SPACES_RE = re.compile(r"\s*&\s*")


def _normalize_match(s: str) -> str:
    """Canonicalise a string for facet matching.

    NFKC folds compatibility variants — notably the full-width ampersand "＆"
    (U+FF06) → "&" — so mixed encodings in the index vocabulary and the query
    collapse before comparison (e.g. "Cities ＆ Infrastructure" matches
    "cities & infrastructure"). Whitespace is collapsed and spacing around "&"
    is unified so "a & b" and "a＆b"/"a&b" also fold together. Lowercased for
    case-insensitive matching. Used for MATCHING only — ``plan`` still returns the
    original index value, so hard filters match the index exactly.
    """
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = _WS_RE.sub(" ", s).strip()
    return _AMP_SPACES_RE.sub("&", s)


def _word_present(value: str, query_norm: str) -> bool:
    """True if ``value`` appears in the (normalised) query on word boundaries.

    ``query_norm`` must already be ``_normalize_match``-ed; the value is
    normalised here so the two sides fold identically (ampersand encodings etc.).
    Word-boundary matching avoids spurious hits like "mali" inside "formalise".
    """
    v = _normalize_match(value).strip()
    if not v:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(v)}(?![a-z0-9])", query_norm) is not None


class FacetPlanner:
    """Extract index-grounded facet filters from a query string."""

    def __init__(
        self,
        *,
        client: "OpenSearch",
        index_name: str,
        facet_fields: tuple[str, ...] = MATCHABLE_FACETS,
    ) -> None:
        self._client = client
        self._index = index_name
        self._facet_fields = facet_fields
        self._vocab: dict[str, list[str]] = {}
        self._vocab_at: float = 0.0

    def _fetch_vocab(self) -> dict[str, list[str]]:
        """Distinct values per facet field (one aggregation call), cached by TTL."""
        now = time.monotonic()
        if self._vocab and (now - self._vocab_at) < _VOCAB_TTL_S:
            return self._vocab
        body = {
            "size": 0,
            "aggs": {
                f: {"terms": {"field": f, "size": _MAX_TERMS_PER_FACET}}
                for f in self._facet_fields
            },
        }
        try:
            resp = self._client.search(index=self._index, body=body)
        except Exception as exc:  # noqa: BLE001 — planning is best-effort
            log.warning("facet_vocab_fetch_failed", index=self._index, error=str(exc))
            return self._vocab  # keep any stale copy rather than nothing
        aggs = resp.get("aggregations") or {}
        self._vocab = {
            f: [b["key"] for b in (aggs.get(f, {}).get("buckets") or []) if b.get("key")]
            for f in self._facet_fields
        }
        self._vocab_at = now
        return self._vocab

    def plan(self, query: str) -> dict[str, list[str]]:
        """Return ``{facet_field: [matched values]}`` for the query (may be empty)."""
        if not query or not query.strip():
            return {}
        query_norm = _normalize_match(query)
        vocab = self._fetch_vocab()
        out: dict[str, list[str]] = {}
        for field, values in vocab.items():
            # Match on the normalised form (ampersand-safe) but RETURN the original
            # index value so hard filters still match the index exactly.
            matched = [v for v in values if _word_present(v, query_norm)]
            if matched:
                out[field] = matched
        # Curated term→field aliases: add a canonical value when its alias term
        # appears in the query (e.g. "d.capital" → dalberg_entity "D. Capital").
        for term, (field, canonical) in _FACET_VALUE_ALIASES.items():
            if _word_present(term, query_norm):
                bucket = out.setdefault(field, [])
                if canonical not in bucket:
                    bucket.append(canonical)
        if out:
            log.debug("facet_filters_derived", index=self._index, filters=out)
        return out
