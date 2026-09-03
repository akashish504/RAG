"""Implementation of the MCP tools.

Source-agnostic by design — adding a new logical source does not require
adding a tool. Every tool returns the same envelope produced by
:class:`retrieval.models.SearchResponse.to_dict`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from retrieval.config import SourceRegistry, get_registry
from retrieval.models import (
    ResponseMode,
    RetrievalQuery,
    SearchResponse,
)
from retrieval.planner.nl_planner import plan_query
from retrieval.router import RetrievalRouter

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(
    response: SearchResponse,
    *,
    text_preview_chars: int | None = None,
) -> str:
    return json.dumps(
        response.to_dict(text_preview_chars=text_preview_chars),
        indent=2,
        default=str,
    )


def _error_envelope(error: str, **diagnostics: Any) -> str:
    return _envelope(SearchResponse(ok=False, error=error, diagnostics=diagnostics))


def _registry() -> SourceRegistry:
    """Live registry with adapters wired (requires PAT / OS env vars)."""

    return get_registry()


def _metadata_registry() -> SourceRegistry:
    """Metadata-only registry — safe to call when secrets are not configured.

    Used by ``list_sources`` and ``get_schema`` so the discovery tools work
    even before the operator has set every adapter's credentials.
    """
    try:
        return get_registry()
    except Exception as exc:  # noqa: BLE001
        log.warning("metadata_registry_fallback", error=str(exc))
        return SourceRegistry.load(instantiate_adapters=False)


def _router() -> RetrievalRouter:
    return RetrievalRouter(registry=_registry())


def _run_async(coro):
    """Synchronous bridge for FastMCP's sync tool functions."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Inside an event loop already (e.g. tests using pytest-asyncio); run
    # in a fresh loop on a worker thread to avoid double-loop errors.
    import concurrent.futures  # noqa: PLC0415

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# ---------------------------------------------------------------------------
# Tool implementations (callable from FastMCP and from Python tests)
# ---------------------------------------------------------------------------


def list_sources_impl() -> str:
    """Return JSON describing every configured source (enabled and disabled).

    Works even if adapter credentials are not configured; missing PAT /
    Voyage env vars degrade to a metadata-only listing.
    """

    try:
        descriptors = _metadata_registry().describe_all()
    except Exception as exc:  # noqa: BLE001
        return _error_envelope(f"Failed to load registry: {exc}")
    return json.dumps({"ok": True, "sources": descriptors}, indent=2, default=str)


def get_schema_impl(source: str) -> str:
    """Return the merged Airtable + OpenSearch schema for one source.

    Works in metadata-only mode — reads the snapshot directly when adapter
    credentials are missing.
    """

    if not source:
        return _error_envelope("`source` is required")
    registry = _metadata_registry()
    try:
        logical = registry.get(source)
    except KeyError as exc:
        return _error_envelope(str(exc))
    try:
        schema = logical.get_schema()
    except Exception:
        # Adapter not wired (e.g. missing PAT). Build a schema directly from
        # the snapshot so discovery still works.
        try:
            schema = _schema_from_snapshot(registry, source)
        except Exception as exc:  # noqa: BLE001
            return _error_envelope(f"Failed to load schema for {source!r}: {exc}")
    return json.dumps({"ok": True, "schema": schema.to_dict()}, indent=2, default=str)


def _schema_from_snapshot(registry: SourceRegistry, source: str):
    """Read an Airtable schema descriptor directly from the snapshot file."""

    from pathlib import Path  # noqa: PLC0415

    from retrieval.paths import REPO_ROOT  # noqa: PLC0415
    from retrieval.sources.airtable import (  # noqa: PLC0415
        _ATTACHMENT_TYPES,
        _LONG_TEXT_TYPES,
    )
    from retrieval.sources.opensearch import (  # noqa: PLC0415
        _SEMANTIC_METADATA_FIELDS,
    )
    from retrieval.models import FieldDescriptor, SchemaDescriptor  # noqa: PLC0415

    src_cfg = registry.config.sources[source]
    if src_cfg.airtable is None:
        from retrieval.sources.opensearch import opensearch_only_schema  # noqa: PLC0415

        return opensearch_only_schema(registry.get(source))
    snapshot_path = src_cfg.airtable.schema_snapshot_path
    if not snapshot_path.is_absolute():
        snapshot_path = REPO_ROOT / snapshot_path
    snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    fields_raw = (snapshot.get("table") or {}).get("fields") or []
    fields: list[FieldDescriptor] = []
    long_text: list[str] = []
    for raw in fields_raw:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        name = str(raw["name"])
        ftype = str(raw.get("type", "unknown"))
        options = raw.get("options") or {}
        choices = None
        if isinstance(options, dict) and isinstance(options.get("choices"), list):
            choices = [
                str(c.get("name"))
                for c in options["choices"]
                if isinstance(c, dict) and c.get("name")
            ] or None
        fields.append(
            FieldDescriptor(
                name=name,
                type=ftype,
                description=raw.get("description") or None,
                select_choices=choices,
                is_long_text=ftype in _LONG_TEXT_TYPES,
                is_attachment=ftype in _ATTACHMENT_TYPES,
                linked_table_id=options.get("linkedTableId") if isinstance(options, dict) else None,
            )
        )
        if ftype in _LONG_TEXT_TYPES:
            long_text.append(name)
    return SchemaDescriptor(
        source=source,
        display_name=src_cfg.display_name,
        description=src_cfg.description,
        capabilities=["semantic", "structured"],
        identifier_field=src_cfg.identifier_field,
        fields=fields,
        long_text_fields=long_text,
        semantic_metadata_fields=list(_SEMANTIC_METADATA_FIELDS),
    )


def semantic_search_impl(
    *,
    query: str,
    source: str | None = None,
    top_k: int = 10,
    filters: dict[str, Any] | None = None,
) -> str:
    """Pure semantic retrieval (KNN + BM25). No Claude call."""

    if not query or not query.strip():
        return _error_envelope("`query` is required and must be non-empty")
    sources = [source] if source else ["*"]
    q = RetrievalQuery(
        question=query.strip(),
        filters=dict(filters or {}),
        sources=sources,
        mode="semantic_only",
        top_k=max(1, min(int(top_k), 50)),
    )
    response = _run_async(_router().run(q))
    return _envelope(response)


def airtable_lookup_impl(
    *,
    source: str,
    formula: str | None = None,
    fields: list[str] | None = None,
    max_records: int | None = None,
) -> str:
    """Pure Airtable retrieval (filterByFormula). No Claude call."""

    if not source:
        return _error_envelope("`source` is required")
    try:
        logical = _registry().get(source)
    except KeyError as exc:
        return _error_envelope(str(exc))
    if logical.airtable is None:
        return _error_envelope(f"source {source!r} has no Airtable adapter")

    q = RetrievalQuery(
        question=None,
        formula=(formula or None),
        fields=list(fields) if fields else None,
        sources=[source],
        mode="airtable_only",
        max_records=max_records,
    )
    response = _run_async(_router().run(q))
    return _envelope(response)


def get_document_link_impl(s3_key: str) -> str:
    """Mint a citation link for one S3 document path. No retrieval, no LLM.

    In short-link mode the token is minted locally (pure HMAC — no S3 call);
    ``/cite/<token>`` re-signs a fresh presigned URL on every click. Scope and
    derived-artifact guards live in :func:`retrieval.citations.build_document_link`.
    """
    if not s3_key or not s3_key.strip():
        return _error_envelope("`s3_key` is required and must be non-empty")

    from pipeline.config import load_settings  # noqa: PLC0415
    from retrieval.citations import build_document_link  # noqa: PLC0415
    from retrieval.settings import get_runtime_settings  # noqa: PLC0415

    try:
        rt = get_runtime_settings()
        bucket = load_settings().s3.bucket
        payload = build_document_link(
            s3_key,
            bucket=bucket,
            signing_secret=rt.citation_signing_secret,
            public_base_url=rt.citation_public_base_url,
            link_ttl_seconds=rt.citation_link_ttl_seconds,
            expiry_seconds=rt.citation_expiry_seconds,
            aws_region=rt.aws_region,
        )
    except ValueError as exc:
        return _error_envelope(str(exc))
    except Exception as exc:  # noqa: BLE001 — settings/config load failures
        return _error_envelope(f"Could not build document link: {exc}")
    return json.dumps({"ok": True, **payload}, indent=2, default=str)


# ---------------------------------------------------------------------------
# Shared constants (used by both search and plan_retrieval)
# ---------------------------------------------------------------------------

# Hard ceiling on max_records returned by any Airtable call in the plan.
# Prevents runaway full-table fetches (the 535-row bug).
_MAX_RECORDS_CAP = 50

# Maximum number of enrichment fields to include per source.
# Keeps the Airtable payload tight while still covering all structured metadata.
_ENRICHMENT_FIELDS_CAP = 20


def plan_retrieval_impl(
    *,
    question: str,
    sources: list[str] | None = None,
) -> str:
    """Schema-aware planner. Returns a plan JSON with suggested_calls; no retrieval is executed."""

    if not question or not question.strip():
        return _error_envelope("`question` is required and must be non-empty")

    registry = _registry()
    requested = sources or ["*"]
    expanded: list[str] = []
    if any(s == "*" for s in requested):
        expanded = registry.names()
    else:
        for name in requested:
            try:
                registry.get(name)
                expanded.append(name)
            except KeyError as exc:
                return _error_envelope(str(exc))

    if not expanded:
        return _error_envelope("No enabled sources to search")

    return _run_async(_plan_retrieval_async(question=question, expanded=expanded, registry=registry))


async def _plan_one_source(source_name: str, question: str, registry: Any) -> dict[str, Any]:
    schema = None
    try:
        logical = registry.get(source_name)
        try:
            schema = logical.get_schema()
        except Exception:
            schema = _schema_from_snapshot(registry, source_name)
        raw_plan = await asyncio.to_thread(plan_query, question=question, schema=schema)
    except Exception as exc:  # noqa: BLE001
        log.warning("planner_failed", source=source_name, error=str(exc))
        raw_plan = {
            "mode": "semantic_only",
            "airtable_formula": "",
            "semantic_query": question.strip(),
            "max_records": None,
            "top_k": 10,
            "rationale": f"Planning failed ({exc}); defaulting to semantic_only.",
            "uncertain": True,
            "lookup_fields": None,
        }
    return _build_source_plan(source_name, question, raw_plan, registry, schema=schema)


async def _plan_retrieval_async(question: str, expanded: list[str], registry: Any) -> str:
    plans = await asyncio.gather(
        *(_plan_one_source(source_name, question, registry) for source_name in expanded)
    )

    return json.dumps(
        {"ok": True, "question": question.strip(), "plans": list(plans)},
        indent=2,
        default=str,
    )


def _enrichment_fields_for_schema(schema: Any) -> list[str] | None:
    """Return structured fields for post-semantic enrichment.

    Always prioritises Skills, Interests, and Languages when present in the schema,
    plus display fields (name, title, office). Excludes long-text and attachments.
    """
    from retrieval.models import SchemaDescriptor  # noqa: PLC0415
    from retrieval.planner.query_classifier import lookup_fields_for_schema  # noqa: PLC0415

    if not isinstance(schema, SchemaDescriptor):
        return None
    fields = lookup_fields_for_schema(schema)
    if not fields:
        return None
    # Append remaining compact structured fields up to the cap.
    names = set(fields)
    for f in schema.fields:
        if f.is_long_text or f.is_attachment or f.name in names:
            continue
        fields.append(f.name)
        names.add(f.name)
        if len(fields) >= _ENRICHMENT_FIELDS_CAP:
            break
    return fields[:_ENRICHMENT_FIELDS_CAP]


def _build_source_plan(
    source_name: str,
    question: str,
    raw_plan: dict[str, Any],
    registry: Any,
    *,
    schema: Any = None,
) -> dict[str, Any]:
    """Construct a per-source plan dict with concrete suggested_calls.

    ``schema`` is a :class:`retrieval.models.SchemaDescriptor` when available.
    Enrichment fields and the identifier field used in the formula template are
    derived from it, making the plan source-agnostic (works for dalberg_profiles,
    knowledge_library, d_quals, or any future source).
    """

    mode: str = str(raw_plan.get("mode") or "semantic_only")
    semantic_query: str = (str(raw_plan.get("semantic_query") or "") or question).strip()
    formula: str = str(raw_plan.get("airtable_formula") or "").strip()
    top_k: int = max(1, min(int(raw_plan.get("top_k") or 10), 50))
    mr_raw = raw_plan.get("max_records")
    max_records: int | None = min(int(mr_raw), _MAX_RECORDS_CAP) if mr_raw else None
    uncertain: bool = bool(raw_plan.get("uncertain"))
    query_kind: str = str(raw_plan.get("query_kind") or "")

    try:
        logical = registry.get(source_name)
        has_airtable = logical.airtable is not None
        has_semantic = logical.opensearch is not None
    except Exception:  # noqa: BLE001
        has_airtable = False
        has_semantic = True

    # Derive identifier field and enrichment fields from the source schema so
    # the plan is correct for any source, not just dalberg_profiles.
    from retrieval.models import SchemaDescriptor  # noqa: PLC0415

    identifier_field: str = (
        schema.identifier_field
        if isinstance(schema, SchemaDescriptor) and schema.identifier_field
        else None
    ) or "id"
    enrichment_fields = _enrichment_fields_for_schema(schema)
    planner_lookup_fields = raw_plan.get("lookup_fields")
    if isinstance(planner_lookup_fields, list) and planner_lookup_fields:
        lookup_fields: list[str] | None = [str(f) for f in planner_lookup_fields]
    else:
        lookup_fields = enrichment_fields

    suggested_calls: list[dict[str, Any]] = []
    step = 1

    if uncertain:
        suggested_calls.append({
            "step": 0,
            "tool": "get_schema",
            "purpose": "verify_fields",
            "note": (
                "Planner mode is uncertain — confirm exact field names and types "
                "before constructing or adjusting airtable_lookup formulas."
            ),
            "args": {"source": source_name},
        })

    # Step 1 — semantic retrieval (KNN + BM25 over embedded chunks)
    if mode in ("semantic_only", "hybrid") and has_semantic and semantic_query:
        suggested_calls.append({
            "step": step,
            "tool": "semantic_search",
            "args": {
                "query": semantic_query,
                "source": source_name,
                "top_k": top_k,
            },
        })
        step += 1

    # Step 2 — structured lookup (only when the planner produced a non-empty formula)
    if mode in ("airtable_only", "hybrid") and has_airtable and formula:
        lookup_args: dict[str, Any] = {
            "source": source_name,
            "formula": formula,
            "max_records": max_records or _MAX_RECORDS_CAP,
        }
        if lookup_fields:
            lookup_args["fields"] = lookup_fields
        suggested_calls.append({
            "step": step,
            "tool": "airtable_lookup",
            "args": lookup_args,
        })
        step += 1

    # Enrichment step — fetch structured metadata fields for semantic hits.
    # The formula must be built by the host from the primary_key values
    # returned by the semantic_search step above.
    # enrichment_fields comes from the source schema (non-long-text, non-attachment
    # fields only) so it is correct for any source, not just dalberg_profiles.
    if mode in ("semantic_only", "hybrid") and has_airtable and has_semantic:
        enrich_args: dict[str, Any] = {
            "source": source_name,
            "formula": (
                f"<replace: OR({{{identifier_field}}}='pk1', {{{identifier_field}}}='pk2', ...) "
                f"— one clause per semantic hit primary_key>"
            ),
            "max_records": _MAX_RECORDS_CAP,
        }
        if enrichment_fields:
            enrich_args["fields"] = enrichment_fields
        suggested_calls.append({
            "step": step,
            "tool": "airtable_lookup",
            "purpose": "enrichment",
            "identifier_field": identifier_field,
            "note": (
                f"After the semantic_search step, extract unique primary_key values "
                f"from hit metadata. Build an OR formula using the source's identifier "
                f"field '{identifier_field}': "
                f"OR({{{identifier_field}}}='pk1', {{{identifier_field}}}='pk2', ...). "
                f"For case-insensitive string identifiers (e.g. emails) wrap with LOWER(): "
                f"OR(LOWER({{{identifier_field}}})='pk1', ...). "
                f"This fetches structured metadata fields for the semantic hits "
                f"without fetching the full table."
            ),
            "args": enrich_args,
        })

    return {
        "source": source_name,
        "mode": mode,
        "query_kind": query_kind or None,
        "semantic_query": semantic_query,
        "airtable_formula": formula,
        "top_k": top_k,
        "max_records": max_records,
        "rationale": str(raw_plan.get("rationale") or "").strip(),
        "suggested_calls": suggested_calls,
    }


# ---------------------------------------------------------------------------
# search — primary all-in-one NL retrieval tool
# ---------------------------------------------------------------------------


def search_impl(
    *,
    question: str,
    sources: list[str] | None = None,
    top_k: int = 10,
) -> str:
    """Plan + execute retrieval in one call. Returns hits, enrichment, and citations."""

    if not question or not question.strip():
        return _error_envelope("`question` is required and must be non-empty")
    return _run_async(
        _search_async(question=question.strip(), sources=sources or ["*"], top_k=top_k)
    )


async def _search_one_source(
    source_name: str,
    question: str,
    top_k: int,
    registry: Any,
    router: RetrievalRouter,
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    logical = registry.get(source_name)
    schema = None
    try:
        try:
            schema = logical.get_schema()
        except Exception:
            schema = _schema_from_snapshot(registry, source_name)
        raw_plan = await asyncio.to_thread(plan_query, question=question, schema=schema)
    except Exception as exc:  # noqa: BLE001
        log.warning("search_plan_failed", source=source_name, error=str(exc))
        raw_plan = {
            "mode": "semantic_only",
            "airtable_formula": "",
            "semantic_query": question,
            "top_k": top_k,
            "max_records": None,
            "uncertain": True,
        }

    mode = str(raw_plan.get("mode") or "hybrid")
    semantic_query = str(raw_plan.get("semantic_query") or question).strip() or question
    formula = str(raw_plan.get("airtable_formula") or "").strip() or None
    # search() ALWAYS runs both retrievals. Whenever the planner/classifier
    # produced a structured formula (e.g. a literal-keyword FIND on
    # Skills/Interests for "who plays piano"), run it ALONGSIDE semantic
    # search rather than choosing airtable_only — structured lookup gets
    # equal weight and the exact match is never missed. (No formula → keep
    # semantic_only, whose bounded enrichment step still hits Airtable.)
    if formula:
        mode = "hybrid"
    plan_top_k = max(1, min(int(raw_plan.get("top_k") or top_k), 50))
    mr_raw = raw_plan.get("max_records")
    max_records: int | None = min(int(mr_raw), _MAX_RECORDS_CAP) if mr_raw else None
    enrichment_fields = _enrichment_fields_for_schema(schema)

    q = RetrievalQuery(
        question=semantic_query,
        formula=formula,
        fields=enrichment_fields,
        sources=[source_name],
        mode=mode,
        top_k=plan_top_k,
        max_records=max_records,
    )
    response = await router.run(q)
    hits: list[Any] = list(response.hits or [])
    hints: list[Any] = list(response.hints or [])

    # Enrich semantic hits with structured profile fields via a separate
    # airtable lookup keyed on primary_key values.
    semantic_hits = [h for h in hits if h.source_type == "semantic"]
    if (
        mode in ("semantic_only", "hybrid")
        and semantic_hits
        and logical.airtable is not None
        and enrichment_fields
    ):
        from retrieval.models import SchemaDescriptor  # noqa: PLC0415

        identifier = (
            schema.identifier_field
            if isinstance(schema, SchemaDescriptor) and schema.identifier_field
            else "id"
        )
        pks = list({
            str(h.metadata.get("primary_key", "")).lower()
            for h in semantic_hits
            if h.metadata.get("primary_key")
        })
        if pks:
            if len(pks) == 1:
                enrich_formula = f"LOWER({{{identifier}}})='{pks[0]}'"
            else:
                clauses = ", ".join(f"LOWER({{{identifier}}})='{pk}'" for pk in pks)
                enrich_formula = f"OR({clauses})"
            enrich_q = RetrievalQuery(
                formula=enrich_formula,
                fields=enrichment_fields,
                sources=[source_name],
                mode="airtable_only",
                max_records=_MAX_RECORDS_CAP,
            )
            enrich_resp = await router.run(enrich_q)
            hits.extend(enrich_resp.hits or [])
            hints.extend(enrich_resp.hints or [])

    diagnostics = {
        "source": source_name,
        "mode": mode,
        "airtable_formula": formula or "",
        "semantic_query": semantic_query,
    }
    return hits, hints, diagnostics


async def _search_async(question: str, sources: list[str], top_k: int) -> str:
    registry = _registry()

    if any(s == "*" for s in sources):
        expanded: list[str] = registry.names()
    else:
        expanded = []
        for name in sources:
            try:
                registry.get(name)
                expanded.append(name)
            except KeyError as exc:
                return _error_envelope(str(exc))

    if not expanded:
        return _error_envelope("No enabled sources to search")

    router = RetrievalRouter(registry=registry)
    results = await asyncio.gather(
        *(
            _search_one_source(source_name, question, top_k, registry, router)
            for source_name in expanded
        )
    )

    all_hits: list[Any] = []
    all_hints: list[Any] = []
    plan_diagnostics: list[dict[str, Any]] = []
    for hits, hints, diagnostics in results:
        all_hits.extend(hits)
        all_hints.extend(hints)
        plan_diagnostics.append(diagnostics)

    # Deduplicate by (source, source_type, text prefix) to avoid double-counting
    # enrichment hits that overlap with direct airtable formula hits.
    seen: set[tuple[str, str, str]] = set()
    deduped: list[Any] = []
    for h in all_hits:
        key = (h.source, h.source_type, (h.text or "")[:80])
        if key not in seen:
            seen.add(key)
            deduped.append(h)

    from retrieval.citations import assign_cite_ids_to_hits, build_references_from_hits  # noqa: PLC0415

    assign_cite_ids_to_hits(deduped)
    references = build_references_from_hits(deduped)

    final = SearchResponse(
        ok=True,
        hits=deduped,
        hints=all_hints,
        references=references,
        response_mode=ResponseMode.FULL_RECORDS,
        diagnostics={"plans": plan_diagnostics},
    )
    return _envelope(final)


# ---------------------------------------------------------------------------
# retrieval_planner — global cross-source orchestration planner
# ---------------------------------------------------------------------------


def retrieval_planner_impl(
    *,
    question: str,
    sources: list[str] | None = None,
    refresh_schema: bool = False,
) -> str:
    """Analyze a query across all sources and return a multi-step execution plan.

    Injects a cached, compact schema digest (fields, identifier/join keys,
    linked tables) per source so the planner can write correct formulas and
    plan cross-table joins without a per-query get_schema round-trip.
    Set ``refresh_schema=True`` to force-rebuild the cached digests.
    """

    if not question or not question.strip():
        return _error_envelope("`question` is required and must be non-empty")

    registry = _metadata_registry()

    # Collect enabled sources (filter to requested names if specified).
    all_sources = registry.enabled_sources()
    if sources:
        requested = set(sources)
        all_sources = [s for s in all_sources if s.name in requested]
    if not all_sources:
        return _error_envelope("No enabled sources available for planning")

    # Build a per-source schema digest from the cached snapshot so the planner
    # sees fields, identifier/join keys, and linked tables without calling
    # get_schema. The loader falls back to the on-disk snapshot when the live
    # adapter is unavailable (metadata-only mode).
    from retrieval.planner.schema_digest import get_cached_digest  # noqa: PLC0415

    def _make_loader(source_name: str):
        def _load():
            logical = registry.get(source_name)
            try:
                return logical.get_schema()
            except Exception:  # noqa: BLE001
                return _schema_from_snapshot(registry, source_name)
        return _load

    sources_summary: list[dict[str, Any]] = [
        get_cached_digest(src.name, _make_loader(src.name), reload=refresh_schema)
        for src in all_sources
    ]

    from retrieval.planner.anthropic_settings import get_anthropic_query_settings  # noqa: PLC0415
    from retrieval.planner.prompts import (  # noqa: PLC0415
        RETRIEVAL_PLANNER_SYSTEM,
        build_retrieval_planner_message,
    )
    import anthropic  # noqa: PLC0415

    settings = get_anthropic_query_settings()
    client = anthropic.Anthropic(api_key=settings.api_key)

    user_msg = build_retrieval_planner_message(
        question=question.strip(),
        sources_summary=sources_summary,
    )

    try:
        msg = client.messages.create(
            model=settings.model,
            max_tokens=settings.max_output_tokens,
            system=[{"type": "text", "text": RETRIEVAL_PLANNER_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = "".join(
            block.text
            for block in msg.content
            if getattr(block, "type", None) == "text" and getattr(block, "text", None)
        ).strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("retrieval_planner_api_failed", error=str(exc))
        return _error_envelope(f"Planner API call failed: {exc}")

    if not raw:
        return _error_envelope("Empty response from planner")

    # Tolerantly extract the JSON object from the response.
    import re as _re  # noqa: PLC0415
    from json import JSONDecodeError, JSONDecoder  # noqa: PLC0415

    text = raw
    fence = _re.search(r"```(?:json)?\s*\n(.*?)```", text, _re.DOTALL | _re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    plan: dict[str, Any] | None = None
    decoder = JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
            if isinstance(obj, dict):
                plan = obj
                break
        except JSONDecodeError:
            continue

    if plan is None:
        return _error_envelope("Planner returned unparseable JSON")

    return json.dumps(
        {
            "ok": True,
            "question": question.strip(),
            "plan": plan,
            "available_sources": [s["name"] for s in sources_summary],
        },
        indent=2,
        default=str,
    )


# ---------------------------------------------------------------------------
# Tool registration with FastMCP
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Bind the tool implementations to a :class:`FastMCP` instance."""

    @mcp.tool()
    def retrieval_planner(
        question: str,
        sources: list[str] | None = None,
        refresh_schema: bool = False,
    ) -> str:
        """[Dalberg Retrieval — Dalberg internal knowledge base] MANDATORY FIRST STEP for every retrieval request — analyses the query and returns a structured execution plan describing which tools to call, in what order, across which sources.

        ROLE: the central orchestrator. Before any retrieval runs, call this
        planner. It inspects the user's intent and ALL available sources,
        then returns a JSON execution plan specifying:
          • query_analysis — intent, query_type, relevant_sources
          • execution_strategy — single | parallel | sequential
          • steps[] — each with tool, args, purpose, depends_on, parallel_group
          • synthesis_guidance — how to combine and present results
          • rationale — why this plan was chosen

        WHEN TO CALL:
          • FIRST, for EVERY natural-language retrieval request — no exceptions.
            The plan decides single-table vs multi-table vs cross-source vs
            hybrid retrieval so you don't have to guess.
          • Whenever a query might span multiple sources (profiles, knowledge
            library, quals, …) or need sequential reasoning.

        WHEN NOT TO CALL:
          • Pure discovery ("what sources exist?") — use list_sources().
          • You already have a plan for this exact question in this session.

        HOW TO USE THE PLAN:
          1. Call retrieval_planner(question=...) → receive plan.
          2. Execute plan.steps in order of parallel_group:
             - same parallel_group → run those tool calls together.
             - depends_on non-empty → wait for those steps; use their output
               to fill this step's args (see dependency_note).
          3. Aggregate all results, dedupe, resolve cross-source relationships.
          4. Synthesise the final answer following synthesis_guidance, keeping
             all citations from the executed tools.

        PARAMETERS:
          • question (str, required) — the user's NL question, verbatim.
          • sources  (list, optional) — restrict planning to these source
            names; omit to let the planner consider all enabled sources.

        Returns JSON: { "ok": true, "question": ..., "plan": {...},
        "available_sources": [...] }.
        """

        return retrieval_planner_impl(
            question=question, sources=sources, refresh_schema=refresh_schema
        )

    @mcp.tool()
    def list_sources() -> str:
        """[Dalberg Retrieval — Dalberg internal knowledge base] List configured retrieval sources (Dalberg knowledge tables) and their capabilities.

        ROLE: discovery. Tells you which Dalberg knowledge tables this server
        indexes right now, their canonical source names, capabilities
        (semantic / airtable / both), display names, and descriptions.

        WHEN TO CALL:
          • First call in any new session, before any retrieval, so you know
            which sources exist and which one matches the user's intent.
          • Whenever the user mentions a Dalberg-domain entity you are not
            sure is indexed (people, quals, proposals, project history, etc.).
          • Before calling get_schema(), search(), semantic_search(), or
            airtable_lookup(source=...) when you don't yet know the source name.
          • The retrieval_planner reads the live source list internally, but
            you may call list_sources() yourself to inspect what exists.

        WHEN NOT TO CALL:
          • Once you already know the source name in this session — re-use it.

        PARAMETERS: none.

        Returns JSON: { "ok": true, "sources": [ { "name": ..., "enabled": ...,
        "capabilities": [...], "display_name": ..., "description": ... }, ... ] }
        """

        return list_sources_impl()

    @mcp.tool()
    def search(
        question: str,
        sources: list[str] | None = None,
        top_k: int = 10,
    ) -> str:
        """[Dalberg Retrieval — Dalberg internal knowledge base] DEFAULT EXECUTOR for broad discovery — answer any NL question across one or many Dalberg sources in one call (plans + retrieves + enriches).

        ROLE: the workhorse executor invoked by retrieval_planner steps. Given
        a NL question, it classifies per source, chooses the optimal mode
        (hybrid / airtable_only / semantic_only), runs semantic KNN+BM25 and/or
        structured Airtable lookup concurrently, enriches semantic hits with
        structured fields, and returns merged, cited hits.

        BROAD DISCOVERY is its sweet spot: "experts in X", "consultants with Y
        experience", "what does Dalberg have on Z". It retrieves candidate
        records across multiple datasets when you pass several sources, and
        returns enough context (chunk text + structured fields) to feed
        downstream retrieval/enrichment steps.

        WHEN TO CALL:
          • As directed by a retrieval_planner step (the normal path).
          • Broad / discovery / open-ended NL questions over one or more sources.
          • Multi-source candidate gathering: pass sources=["a", "b", ...].

        ITERATIVE REFINEMENT (do not stop on weak results):
          • If hits are sparse, low-scoring, or off-topic, run a
            semantic_search() pass with a rephrased, concept-level query, then
            combine and rerank.
          • If a structured attribute is missing from hits, follow up with
            airtable_lookup() (after get_schema()) to enrich the winners.
          • For multi-part questions, issue an additional search() per
            uncovered sub-part rather than answering partially.

        WHEN NOT TO CALL:
          • You skipped planning — call retrieval_planner() first.
          • Exact id/email/name lookup → get_schema() then airtable_lookup().
          • Discovery of which sources/fields exist → list_sources()/get_schema().

        PARAMETERS:
          • question  (str, required)  — NL question verbatim.
          • sources   (list, optional) — source names from list_sources().
                                         Omit or ["*"] to search all sources.
          • top_k     (int, default 10, max 50) — number of semantic hits.

        CITATIONS (required):
          • Each hit has citation_url → a link to the source document (S3
            presigned URL) or structured record. Include it.
          • Paste references_markdown verbatim in your reply.
          • Never invent URLs; only use citation_url from the response.

        CONFIDENTIALITY TAGS (required, d_quals hits):
          • If a hit's text starts with **[CONFIDENTIAL PROJECT]**, **[CONFIDENTIAL
            CLIENT]**, or **[CONFIDENTIAL PROJECT & CLIENT]**, reproduce that
            exact tag verbatim next to the project's name in your reply.
            Never paraphrase it away or omit it.
          • Render it as actual BOLD markdown text (the ** are already in the
            tag — output them as-is, not in a code block/backticks, and don't
            strip them). The reader must see bold text, not literal asterisks.
          • The tag is a label only — still include the citation_url/link and
            every other field in full. Never withhold a link or tell the user
            to contact someone else for the documents because of this tag.
            Same rule if you see raw "Confidential Project"/"Confidential
            Client" fields in metadata instead of the tag — showing the tag
            is the ONLY behavior change; nothing else is ever withheld.

        Returns JSON with hits[], references_markdown, and diagnostics.plans[]
        showing the mode and formula chosen per source.
        """

        return search_impl(question=question, sources=sources, top_k=top_k)

    @mcp.tool()
    def get_schema(source: str) -> str:
        """[Dalberg Retrieval — Dalberg internal knowledge base] Return the field schema for a Dalberg Retrieval source — call before writing an airtable_lookup formula.

        ROLE: schema introspection for one source. Lists every field's exact
        name, type, select choices, and whether it is long-text (truncated by
        airtable_lookup — use semantic_search for full text).

        WHEN TO CALL:
          • ALWAYS before writing an airtable_lookup formula, so {Field Name}
            references match the catalog exactly (Airtable formulas fail on
            unknown / misspelled fields).
          • When the user asks "what fields are available on <source>?"
          • When choosing which fields to request via airtable_lookup.fields.

        WHEN NOT TO CALL:
          • For pure semantic searches via semantic_search() — the KNN does
            not need a manual schema lookup.
          • search() and retrieval_planner() read schema/source info
            internally — but ALWAYS call get_schema() yourself before writing
            an airtable_lookup formula when fields are unknown or may have changed.

        PARAMETERS:
          • source (str, required) — source name from list_sources().

        Example workflow:
          1. list_sources() → discover source name (e.g. "dalberg_profiles").
          2. get_schema("dalberg_profiles") → learn fields and select choices.
          3. airtable_lookup(source="dalberg_profiles",
                             formula='FIND("French", {Languages})').

        Returns JSON with fields[] describing name, type, select_choices, and
        whether the field is long-text.
        """

        return get_schema_impl(source)

    @mcp.tool()
    def semantic_search(
        query: str,
        source: str | None = None,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> str:
        """[Dalberg Retrieval — Dalberg internal knowledge base] Concept-based vector (KNN + BM25) search over embedded chunks — the SECOND-PASS / fallback retriever for weak, sparse, ambiguous, or loosely-specified queries.

        ROLE: low-level concept-matching retrieval over embedded text chunks
        of ONE source. Returns raw chunk text + metadata — no planning, no
        enrichment, no synthesised answer. Matches MEANING and intent, NOT
        exact keywords — so it surfaces relevant passages that keyword or
        structured search miss.

        WHEN TO CALL:
          • As directed by a retrieval_planner step (often a fallback step).
          • SECOND PASS when search() / structured lookup returned weak,
            sparse, low-confidence, or ambiguous results — rephrase the query
            at a concept level and retry here.
          • CONCEPTUAL / descriptive / loosely-specified queries where the
            user describes an idea rather than exact terms.
          • You need raw chunk text (CV paragraphs, document passages) to
            quote or summarise.

        HYBRID STRATEGY (Search + Semantic Search complement each other):
          • If both keyword/structured search and semantic search return
            useful hits, COMBINE and rerank by relevance before answering.
          • Treat the two as recall-boosting passes, not either/or.

        WHEN NOT TO CALL:
          • Open-ended request without planning → call retrieval_planner first.
          • Exact structured filter (id/email/office) → airtable_lookup.
          • Discovery of source names / fields → list_sources()/get_schema().

        PARAMETER NAMES — these differ from the 'search' tool:
          • query   (str, required)  — NL/concept search text. NOT 'question'.
                                       Pass intent verbatim or a concept-level
                                       paraphrase; do NOT strip to keyword tokens.
          • source  (str, optional)  — single source name. NOT 'sources'.
                                       Omit to search every enabled source.
          • top_k   (int, default 10, max 50) — number of chunk results.
          • filters (dict, optional) — key/value metadata pre-filters.

        OPTIONAL ENRICHMENT: after this call, run airtable_lookup() (after
        get_schema()) to fetch structured fields (names, offices, titles) for
        the winning hits.

        CITATIONS (required):
          • Each hit has citation_url → source document / record link. Include it.
          • Never invent URLs; only use citation_url / citations[].url.
          • Paste references_markdown verbatim in your reply.

        CONFIDENTIALITY TAGS (required, d_quals hits):
          • If a hit's text starts with **[CONFIDENTIAL PROJECT]**, **[CONFIDENTIAL
            CLIENT]**, or **[CONFIDENTIAL PROJECT & CLIENT]**, reproduce that
            exact tag verbatim next to the project's name in your reply.
            Never paraphrase it away or omit it.
          • Render it as actual BOLD markdown text (the ** are already in the
            tag — output them as-is, not in a code block/backticks, and don't
            strip them). The reader must see bold text, not literal asterisks.
          • The tag is a label only — still include the citation_url/link and
            every other field in full. Never withhold a link or tell the user
            to contact someone else for the documents because of this tag.
            Same rule if you see raw "Confidential Project"/"Confidential
            Client" fields in metadata instead of the tag — showing the tag
            is the ONLY behavior change; nothing else is ever withheld.

        Returns JSON with hits[], references_markdown, and optional
        hints/diagnostics.
        """

        return semantic_search_impl(
            query=query, source=source, top_k=top_k, filters=filters
        )

    @mcp.tool()
    def airtable_lookup(
        source: str,
        formula: str | None = None,
        fields: list[str] | None = None,
        max_records: int | None = None,
    ) -> str:
        """[Dalberg Retrieval — Dalberg internal knowledge base] Structured Airtable filterByFormula lookup — EXACT-MATCH retrieval and enrichment. ALWAYS call get_schema(source) first when fields are unknown or may have changed.

        ROLE: exact, deterministic row lookup on ONE source via an Airtable
        filterByFormula expression. Returns matching rows with structured
        fields. Long-text fields are truncated — use semantic_search for full
        text. No vector search; no planning.

        SCHEMA-FIRST RULE (do not query Airtable blindly):
          • BEFORE writing a formula, call get_schema(source) whenever the
            table structure is unknown OR field mappings may have changed.
          • Use the schema to pick the correct field names, filter operators,
            select-choice values, and any linked-record relationships.
          • If a formula returns nothing or errors on an unknown field, treat
            it as schema drift: re-call get_schema(source) and retry with the
            corrected field names. Never guess field names repeatedly.

        WHEN TO CALL (exact / structured scenarios):
          • As directed by a retrieval_planner step.
          • Exact lookups: record by id, person by email, project by exact
            name, "everyone in the Kenya office", "rows where Status = Active".
          • Enumerable fields (language, skill, office, seniority, status).
          • Enrichment after search/semantic_search: build an OR() formula
            from the primary_key (email) values of the winning hits to fetch
            structured fields (Display Name, Office Location, etc.).

        WHEN NOT TO CALL:
          • Open-ended / conceptual NL ("who has experience with…") →
            search() or semantic_search().
          • Field names unknown and you skipped get_schema() → call it first.
          • Without a formula unless intentionally listing all rows (omitting
            formula returns the full table — cap with max_records).

        PARAMETERS:
          • source       (str, required)  — source name from list_sources().
          • formula      (str, optional)  — Airtable filterByFormula using
                                            EXACT {Field Name} from get_schema().
          • fields       (list, optional) — field names to return; prefer an
                                            explicit list to keep payloads small.
          • max_records  (int, optional)  — cap on rows returned.

        Formula examples (replace placeholders with real field names from
        get_schema):
          FIND("<value>", LOWER({<MultiSelect Field>}))   ← case-insensitive
          AND({<Field A>}="<x>", {<Field B>}="<y>")
          OR(LOWER({<IdField>})='<a>', LOWER({<IdField>})='<b>')

        CITATIONS (required):
          • Each row has citation_url → source record link. Include it.
          • Never invent URLs; only use citation_url from the response.

        CONFIDENTIALITY TAGS (required, d_quals hits):
          • If a hit's text starts with **[CONFIDENTIAL PROJECT]**, **[CONFIDENTIAL
            CLIENT]**, or **[CONFIDENTIAL PROJECT & CLIENT]**, reproduce that
            exact tag verbatim next to the project's name in your reply.
            Never paraphrase it away or omit it.
          • Render it as actual BOLD markdown text (the ** are already in the
            tag — output them as-is, not in a code block/backticks, and don't
            strip them). The reader must see bold text, not literal asterisks.
          • The tag is a label only — still include the citation_url/link and
            every other field in full. Never withhold a link or tell the user
            to contact someone else for the documents because of this tag.
            Same rule if you see raw "Confidential Project"/"Confidential
            Client" fields in metadata instead of the tag — showing the tag
            is the ONLY behavior change; nothing else is ever withheld.

        Returns JSON with hits[], references_markdown, and diagnostics.
        """

        return airtable_lookup_impl(
            source=source,
            formula=formula,
            fields=fields,
            max_records=max_records,
        )

    @mcp.tool()
    def get_document_link(s3_key: str) -> str:
        """[Dalberg Retrieval — Dalberg internal knowledge base] Mint a durable, clickable link to a SOURCE DOCUMENT from its S3 path — call with a `source_s3_key` value returned in a hit's metadata.

        ROLE: link generation only — no retrieval, no LLM, near-instant. Use it
        to attach a citation link to any document you reference, without every
        hit having to carry link plumbing.

        WHEN TO CALL:
          • You are citing a document from a search/semantic_search hit and
            need its clickable link: pass that hit's metadata.source_s3_key.
          • The user asks for "the link to" a document you already retrieved.

        WHEN NOT TO CALL:
          • You don't have an s3 path — retrieve first; never guess a path.
          • For Airtable records — records are not documents; there are no
            Airtable links.

        PARAMETERS:
          • s3_key (str, required) — the document path under raw/, exactly as
            returned in hit metadata (source_s3_key). A full s3:// URL is also
            accepted.

        GUARANTEES / ERRORS:
          • Only paths under raw/ are allowed (no traversal).
          • Derived artifacts (.txt extraction/summary files) are refused —
            only original source documents (PDF/PPTX/DOCX/…) are citable.

        Returns JSON: { "ok": true, "url": ..., "mode": "short_link"|"presigned",
        "expires_in_seconds": ..., "s3_key": ... }. Use `url` as the citation
        link exactly as returned; never invent or modify it.
        """

        return get_document_link_impl(s3_key)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _allowed_field_names_for(
    registry: SourceRegistry,
    sources: list[str],
) -> list[str]:
    names: set[str] = set()
    for s in sources:
        try:
            schema = registry.get(s).get_schema()
        except Exception:  # noqa: BLE001
            continue
        names.update(f.name for f in schema.fields)
    return sorted(names)


__all__ = [
    "register_tools",
    "retrieval_planner_impl",
    "list_sources_impl",
    "get_schema_impl",
    "search_impl",
    "semantic_search_impl",
    "airtable_lookup_impl",
    "get_document_link_impl",
    "plan_retrieval_impl",
]
