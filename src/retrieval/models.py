"""Core data shapes shared across the retrieval module.

These types form the boundary contract between MCP tools, the router, and
individual :class:`retrieval.sources.base.RetrievalSource` adapters. Every
MCP tool returns a :class:`SearchResponse`; every adapter returns a list of
:class:`SearchResult` plus optional :class:`Hint` objects.

Design notes
------------
- ``SearchResult.score`` is normalised to ``[0, 1]`` per source. The merger
  re-ranks across sources via Reciprocal Rank Fusion, so per-source absolute
  scores do not need to be comparable.
- ``Hint`` is a structured cross-tool follow-up. Adapters emit hints when
  they hit a boundary that another tool can solve (e.g. an Airtable
  long-text field is truncated -> ``semantic_search`` can fetch the full
  text from chunks).
- ``ResponseMode`` is the same vocabulary used by today's
  :mod:`pipeline.api.nl_response_shape` so behaviour is preserved during
  the migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ResponseMode(StrEnum):
    """How the formatter should present results to the caller."""

    COUNT_ONLY = "count_only"
    COLUMN_SUBSET = "column_subset"
    FULL_RECORDS = "full_records"


SourceType = Literal["semantic", "structured"]
SearchMode = Literal["semantic_only", "airtable_only", "hybrid"]
CitationKind = Literal["airtable_record", "document_section", "document"]

# Metadata keys stripped from MCP-facing hit envelopes (kept in OpenSearch / payload).
# These fields support indexing and pipeline debugging only — never expose them in MCP
# responses. User-facing citations use citation_url / citations[].url (Airtable).
_REDACTED_METADATA_KEYS = frozenset({
    # S3 / storage internals
    "s3_key",
    "s3_bucket",
    "s3_path",
    "source_url",
    "filename",
    # OpenSearch / Airtable pipeline internals
    "table_name",
    "airtable_record_id",
    "airtable_base_id",
    "airtable_table_id",
    "position",
    "token_count",
    "embedding_model",
    "indexed_at",
    # Chunker / indexing internals (nested metadata blob from resume chunker)
    "document_id",
    "chunk_index",
    "child_index_in_parent",
    "section_path",
    "section_level",
    "section_title",
    "is_list_section",
    "strategy",
    "entry_index",
    "column_name",
})

# Locator keys kept in MCP citation output (human-readable context only).
# source_s3_key / source_s3_url expose the ORIGINAL document's S3 location so
# callers can see/verify the cited source (the presigned citation_url is the
# accessible link; these are the path).
_MCP_LOCATOR_KEYS = frozenset(
    {"attachment_column", "section_canonical", "source_s3_key", "source_s3_url"}
)


# ---------------------------------------------------------------------------
# Query + result shapes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RetrievalQuery:
    """Normalised query passed from the router into each adapter.

    Adapters that cannot satisfy a particular field (e.g. Airtable cannot
    use ``embedding``) silently ignore it. The router records dropped
    filters in :attr:`SearchResponse.diagnostics` for observability.
    """

    question: str | None = None
    embedding: list[float] | None = None
    formula: str | None = None
    fields: list[str] | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    mode: SearchMode = "hybrid"
    top_k: int = 10
    max_records: int | None = None


@dataclass(slots=True)
class Citation:
    """Server-built source attribution for one piece of evidence."""

    cite_id: str
    kind: CitationKind
    label: str
    url: str | None = None
    locator: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        locator = {
            k: v
            for k, v in self.locator.items()
            if k in _MCP_LOCATOR_KEYS and v is not None
        }
        return {
            "cite_id": self.cite_id,
            "kind": self.kind,
            "label": self.label,
            "url": self.url,
            "locator": locator,
        }


# Client-facing locator keys that must never carry a derived ``.txt`` artifact
# path (record_summary / normalized extraction text). A legitimate source is a
# PDF/PPTX/DOCX, never a ``.txt`` — so a ``.txt`` value here is always a leak.
_TXT_GUARDED_LOCATOR_KEYS = frozenset({"source_s3_key", "source_s3_url"})


def redact_metadata_for_response(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return metadata safe for MCP clients (no internal S3 paths).

    Hard guarantee (belt-and-suspenders with the citation resolver): a
    ``source_s3_key``/``source_s3_url`` whose value ends in ``.txt`` is a derived
    artifact path and is dropped, so a ``.txt`` "source" can never reach the
    client regardless of which retrieval path produced the hit.
    """

    out: dict[str, Any] = {}
    for k, v in metadata.items():
        if k in _REDACTED_METADATA_KEYS or v is None:
            continue
        if k in _TXT_GUARDED_LOCATOR_KEYS and isinstance(v, str) and v.endswith(".txt"):
            continue
        out[k] = v
    return out


@dataclass(slots=True)
class SearchResult:
    """One hit from one source. Backend-native ``payload`` is preserved."""

    source: str
    source_type: SourceType
    score: float
    text: str
    chunk_id: str | None = None
    record_id: str | None = None
    parent_chunk_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    # Direct link to the source record (Airtable); backward-compatible primary URL.
    citation_url: str | None = None
    citations: list[Citation] = field(default_factory=list)

    # Maximum characters included in `text` when serialising to the MCP
    # response. Full text is kept in memory for the synthesis pass but never
    # sent to Claude verbatim — it sends a 600-char excerpt + metadata instead.
    # Raise this if Claude needs more context per hit; lower it to save tokens.
    TEXT_PREVIEW_CHARS: int = 800

    def to_dict(
        self,
        *,
        text_preview_chars: int | None = None,
        include_citations_array: bool = True,
    ) -> dict[str, Any]:
        limit = text_preview_chars if text_preview_chars is not None else self.TEXT_PREVIEW_CHARS
        text = self.text or ""
        preview = text[:limit] + ("…" if len(text) > limit else "")
        out: dict[str, Any] = {
            "source": self.source,
            "source_type": self.source_type,
            "score": round(self.score, 4),
            "text": preview,
            "metadata": redact_metadata_for_response(self.metadata),
            "citation_url": self.citation_url,
            # chunk_id, parent_chunk_id, record_id, text_truncated are
            # internal pipeline fields — omitted to save Claude context tokens.
            # payload excluded: raw backend docs only used for internal synthesis.
        }
        if include_citations_array:
            out["citations"] = [c.to_dict() for c in self.citations]
        return out


@dataclass(slots=True)
class Hint:
    """Cross-tool follow-up suggestion surfaced to the MCP client.

    Examples
    --------
    Airtable truncated a long-text field::

        Hint(tool="semantic_search", source="dalberg_profiles",
             reason="long_text_field_truncated",
             detail={"field": "Bio Text"})

    OpenSearch hit carries an Airtable record id::

        Hint(tool="airtable_lookup", source="dalberg_profiles",
             reason="structured_fields_available",
             detail={"record_id": "rec123"})
    """

    tool: str
    source: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "source": self.source,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(slots=True)
class SearchResponse:
    """Uniform envelope returned by every MCP tool."""

    ok: bool
    hits: list[SearchResult] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)
    response_mode: ResponseMode = ResponseMode.FULL_RECORDS
    columns: list[str] = field(default_factory=list)
    markdown_table: str | None = None
    answer: str | None = None
    plan: dict[str, Any] | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    references: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(
        self,
        *,
        text_preview_chars: int | None = None,
    ) -> dict[str, Any]:
        from retrieval.citations import (  # noqa: PLC0415
            ASSISTANT_CITATION_INSTRUCTION,
            build_citation_resolve_warning,
            build_markdown_references_section,
            build_references_from_hits,
        )

        refs = self.references
        if not refs and self.hits:
            refs = build_references_from_hits(self.hits)
        refs_md = build_markdown_references_section(refs)
        # When references_markdown is present every hit's citation data is
        # already encoded there + on hit.citation_url — drop the redundant
        # per-hit citations[] array from the wire payload.
        include_hit_citations = not bool(refs_md)

        # Trim diagnostics: only surface the citations sub-key to Claude.
        # Latency, embedding details, and merge strategy are internal.
        slim_diagnostics: dict[str, Any] = {}
        if self.diagnostics.get("citations"):
            slim_diagnostics["citations"] = self.diagnostics["citations"]
        if self.diagnostics.get("error"):
            slim_diagnostics["error"] = self.diagnostics["error"]
        if self.diagnostics.get("plans"):
            slim_diagnostics["plans"] = self.diagnostics["plans"]
        # Bubble up per-source errors so Claude can act on them.
        sources_diag = self.diagnostics.get("sources") or {}
        source_errors = {
            src: d.get("error")
            for src, d in sources_diag.items()
            if isinstance(d, dict) and d.get("error")
        }
        if source_errors:
            slim_diagnostics["source_errors"] = source_errors

        out: dict[str, Any] = {
            "ok": self.ok,
            "hits": [
                h.to_dict(
                    text_preview_chars=text_preview_chars,
                    include_citations_array=include_hit_citations,
                )
                for h in self.hits
            ],
        }

        # Include hints only when present — they guide follow-up tool calls.
        if self.hints:
            out["hints"] = [h.to_dict() for h in self.hints]

        # references_markdown is the user-facing citation block; include it and
        # drop the raw references[] array (redundant when markdown is present).
        if refs_md:
            out["references_markdown"] = refs_md
            out["assistant_instruction"] = ASSISTANT_CITATION_INSTRUCTION
        elif refs:
            # No markdown (e.g. no citation_url on any hit) — keep raw refs as fallback.
            out["references"] = refs

        # answer is the synthesised narrative; include when non-empty.
        if self.answer:
            out["answer"] = self.answer

        # markdown_table and columns only when a table was actually generated.
        if self.markdown_table:
            out["markdown_table"] = self.markdown_table
            if self.columns:
                out["columns"] = self.columns

        # diagnostics: only include when there is something actionable.
        if slim_diagnostics:
            out["diagnostics"] = slim_diagnostics

        # error only when present.
        if self.error:
            out["error"] = self.error

        # plan and hint_count omitted: internal planning data not needed by Claude.

        warn = build_citation_resolve_warning(self.diagnostics)
        if warn:
            out["citation_resolve_warning"] = warn
        return out


# ---------------------------------------------------------------------------
# Schema descriptor
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FieldDescriptor:
    """One field in a source's schema, normalised across backends."""

    name: str
    type: str
    description: str | None = None
    select_choices: list[str] | None = None
    is_long_text: bool = False
    is_attachment: bool = False
    linked_table_id: str | None = None


@dataclass(slots=True)
class SchemaDescriptor:
    """Source-agnostic schema description used by the planner and tools.

    Built from the Airtable snapshot for ``structured`` capabilities and
    augmented with the OpenSearch metadata fields for ``semantic`` ones.
    """

    source: str
    display_name: str
    description: str | None
    capabilities: list[SourceType]
    identifier_field: str | None
    fields: list[FieldDescriptor]
    long_text_fields: list[str] = field(default_factory=list)
    semantic_metadata_fields: list[str] = field(default_factory=list)

    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "display_name": self.display_name,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "identifier_field": self.identifier_field,
            "long_text_fields": list(self.long_text_fields),
            "semantic_metadata_fields": list(self.semantic_metadata_fields),
            "fields": [
                {
                    "name": f.name,
                    "type": f.type,
                    "description": f.description,
                    "select_choices": f.select_choices,
                    "is_long_text": f.is_long_text,
                    "is_attachment": f.is_attachment,
                    "linked_table_id": f.linked_table_id,
                }
                for f in self.fields
            ],
        }
