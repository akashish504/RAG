"""Build traceable citations for retrieval hits.

Default (semantic hits): S3 presigned URLs generated from ``s3_key`` / ``s3_bucket``
in chunk metadata — links open the original source document directly.

Legacy (structured Airtable hits and explicit Airtable lookups): Airtable record
URLs via ``CitationResolver``.  The Airtable path is preserved intact and can be
used whenever the caller needs it (e.g. a tool that explicitly fetches Airtable rows).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from retrieval.models import Citation, SearchResult

if TYPE_CHECKING:
    from retrieval.sources.airtable import AirtableSource

log = structlog.get_logger(__name__)

# Default slug → Airtable column display names (Dalberg Profiles attachments).
_DEFAULT_COLUMN_SLUG_MAP: dict[str, str] = {
    "cv_attachment": "CV Attachment",
    "bio_attachment": "Bio Attachment",
}

# Max emails per Airtable filterByFormula OR batch (formula length limits).
_BATCH_LOOKUP_SIZE = 20


def build_airtable_record_url(
    *,
    base_id: str,
    table_id: str,
    record_id: str,
) -> str:
    return f"https://airtable.com/{base_id}/{table_id}/{record_id}"


def escape_airtable_formula_string(value: str) -> str:
    """Escape a value for use inside single-quoted Airtable formula strings."""

    return (value or "").replace("\\", "\\\\").replace("'", "\\'")


def build_identifier_formula(field_name: str, identifier_value: str) -> str:
    """Case-insensitive match — OpenSearch primary_key is lowercased from S3 paths."""

    escaped = escape_airtable_formula_string(identifier_value.strip().lower())
    return f"LOWER({{{field_name}}}) = '{escaped}'"


def build_batch_identifier_formula(field_name: str, identifier_values: list[str]) -> str:
    """OR of LOWER({field}) = 'email' clauses for batch record lookup."""

    clauses = [
        build_identifier_formula(field_name, value) for value in identifier_values if value.strip()
    ]
    if not clauses:
        return ""
    if len(clauses) == 1:
        return clauses[0]
    return "OR(" + ",".join(clauses) + ")"


def column_display_name(
    column_slug: str | None,
    *,
    slug_map: dict[str, str] | None = None,
) -> str | None:
    if not column_slug:
        return None
    mapping = slug_map or _DEFAULT_COLUMN_SLUG_MAP
    key = column_slug.strip().lower().replace("-", "_")
    if key in mapping:
        return mapping[key]
    # Fallback: cv_attachment → Cv Attachment style
    return column_slug.replace("_", " ").title()


def _display_name_from_metadata(metadata: dict[str, Any]) -> str | None:
    for key in ("Display Name", "display_name", "Name", "name"):
        val = metadata.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def build_semantic_citation_label(
    metadata: dict[str, Any],
    *,
    slug_map: dict[str, str] | None = None,
    display_name: str | None = None,
) -> str:
    """Human label: ``Jane Doe — CV Attachment (Experience)``."""

    person = (
        display_name
        or _display_name_from_metadata(metadata)
        or metadata.get("primary_key")
        or "Profile"
    )
    col = column_display_name(
        metadata.get("column_name") if isinstance(metadata.get("column_name"), str) else None,
        slug_map=slug_map,
    )
    section = metadata.get("section_canonical")
    if col and section:
        return f"{person} — {col} ({section})"
    if col:
        return f"{person} — {col}"
    if section:
        return f"{person} ({section})"
    return str(person)


_NORMALIZED_SUFFIX = "__normalized.txt"
_META_SUFFIX = ".airtable_meta.json"

# GUARDRAIL: any ``.txt`` under raw/ is a DERIVED pipeline artifact (normalized
# extraction text, __record_summary.txt, …) — never a citation target. Citations
# must point at original source documents (PDF/PPTX/DOCX/…) only.
_DERIVED_TEXT_SUFFIX = ".txt"

# doc_role values marking derived summary chunks. They are embedded for recall
# and hydrated as context, but must NEVER be cited — there is no single source
# document behind them.
_DERIVED_DOC_ROLES = frozenset({"record_summary", "deck_summary"})


def _clear_source_path(hit: SearchResult) -> None:
    """Strip the client-facing S3 locator keys from a suppressed hit's metadata.

    ``source_s3_key`` / ``source_s3_url`` are NOT in ``_REDACTED_METADATA_KEYS``,
    so they reach the client. For a derived/unresolved hit (record_summary,
    deck_summary, or a chunk whose only key is a ``__…​.txt`` artifact) that value
    is a derived ``.txt`` path — clearing it guarantees no ``.txt`` "source"
    surfaces alongside a nulled citation_url.
    """
    hit.metadata.pop("source_s3_key", None)
    hit.metadata.pop("source_s3_url", None)


def _resolve_original_s3_key(
    s3_key: str,
    *,
    bucket: str,
    region_name: str,
    cache: dict[str, str | None],
) -> str | None:
    """Resolve the ORIGINAL source-document key for a chunk's indexed ``s3_key``.

    The embedding pipeline indexes the normalized text key
    (``{key_dir}/{identifier}__normalized.txt``), but the original attachment is
    stored under the same directory as ``{key_dir}/{attachment_id}__{filename}``.
    The attachment id is NOT recoverable from the normalized key by string math,
    so we LIST the directory and pick the file that is neither a derived ``.txt``
    nor the Airtable metadata sidecar.

    Returns the original document key, or ``None`` when the directory holds no
    original — the caller then emits NO citation rather than linking a derived
    ``.txt`` artifact (guardrail: citations point at source documents only).

    ``cache`` memoises per directory within a single request.
    """
    if not s3_key.endswith(_DERIVED_TEXT_SUFFIX):
        return s3_key  # already an original file (e.g. a PDF indexed directly)

    key_dir = s3_key.rsplit("/", 1)[0] + "/"
    if key_dir in cache:
        return cache[key_dir]

    from pipeline.common.aws import list_s3_keys  # noqa: PLC0415

    original: str | None = None  # guardrail: never fall back to the .txt itself
    for key in list_s3_keys(key_dir, bucket=bucket, region_name=region_name):
        leaf = key.rsplit("/", 1)[-1]
        if leaf.endswith(_DERIVED_TEXT_SUFFIX) or leaf.endswith(_META_SUFFIX):
            continue
        original = key  # the original attachment (PDF/DOCX/PPTX/…)
        break

    cache[key_dir] = original
    return original


def active_citation_mode(
    *, signing_secret: str | None, public_base_url: str | None
) -> tuple[str, str | None]:
    """Return the citation delivery mode for the given config: one source of truth
    shared by ``S3CitationResolver`` and the api boot-time guard.

    Returns ``("short_link", base_url)`` when a secret AND a valid http(s) base URL
    are set — links are ``<base>/cite/<token>`` re-signed fresh on each click
    (robust to rotating EC2 credentials). Otherwise ``("presigned", base_or_None)`` —
    raw presigned S3 URLs, which break with ``InvalidToken`` once temporary role
    credentials rotate.
    """
    base = _sanitize_base_url(public_base_url)
    if signing_secret and base:
        return "short_link", base
    return "presigned", base


def _sanitize_base_url(value: str | None) -> str | None:
    """Return a usable public base URL, or None to fall back to presigned S3 URLs.

    Treats a blank value, an unfilled placeholder (e.g. ``http://<ec2-public-dns>``),
    or a non-http(s) string as UNSET — otherwise short-link mode would emit dead
    ``/cite/<token>`` URLs pointing at a host that doesn't exist.
    """
    v = (value or "").strip().rstrip("/")
    if not v or "<" in v or ">" in v or not v.lower().startswith(("http://", "https://")):
        return None
    return v


def build_document_link(
    s3_key: str,
    *,
    bucket: str,
    signing_secret: str | None,
    public_base_url: str | None,
    link_ttl_seconds: int = 86400,
    expiry_seconds: int = 3600,
    aws_region: str = "eu-west-1",
) -> dict[str, Any]:
    """Mint a citation link for ONE S3 document key (the ``get_document_link``
    MCP tool). Lets the model request links only for documents it actually
    cites, instead of every hit carrying URL plumbing.

    Short-link mode (secret + base URL configured): the token is minted locally
    (pure HMAC — zero S3/network calls) and ``/cite/<token>`` re-signs a fresh
    presigned URL on every click, so the link survives credential rotation.
    Fallback: a raw presigned URL, flagged with a fragility warning.

    Raises ``ValueError`` for out-of-scope keys (must live under ``raw/``, no
    traversal — mirrors the ``/cite`` endpoint guard) and for derived ``.txt``
    artifacts (same guardrail as the resolver: only source documents are
    citable; callers should pass a hit's ``source_s3_key``).
    """
    key = (s3_key or "").strip().lstrip("/")
    if key.startswith("s3://"):  # tolerate a full s3:// URL pasted back in
        rest = key[len("s3://"):]
        key = rest.split("/", 1)[1] if "/" in rest else ""
    if not key.startswith("raw/") or ".." in key:
        raise ValueError(
            "s3_key must be a document path under 'raw/' (no traversal) — "
            "use the source_s3_key value returned in a hit's metadata"
        )
    if key.endswith(_DERIVED_TEXT_SUFFIX):
        raise ValueError(
            "derived artifact (.txt) — only original source documents are "
            "citable; use the hit's source_s3_key"
        )

    mode, base = active_citation_mode(
        signing_secret=signing_secret, public_base_url=public_base_url
    )
    if mode == "short_link":
        from retrieval.citation_token import make_citation_token  # noqa: PLC0415

        token = make_citation_token(
            key, bucket=bucket, ttl_seconds=link_ttl_seconds,
            secret=signing_secret,  # type: ignore[arg-type]
        )
        return {
            "url": f"{base}/cite/{token}",
            "mode": "short_link",
            "expires_in_seconds": link_ttl_seconds,
            "s3_key": key,
        }

    from pipeline.common.aws import generate_presigned_url  # noqa: PLC0415

    url = generate_presigned_url(
        key, bucket=bucket, expiry_seconds=expiry_seconds, region_name=aws_region
    )
    if not url:
        raise ValueError("could not sign the document URL (check AWS credentials)")
    return {
        "url": url,
        "mode": "presigned",
        "expires_in_seconds": expiry_seconds,
        "s3_key": key,
        "warning": (
            "raw presigned URL — expires and can break when server credentials "
            "rotate; set CITATION_SIGNING_SECRET + CITATION_PUBLIC_BASE_URL for "
            "durable short links"
        ),
    }


class S3CitationResolver:
    """Populate ``citation_url`` on semantic hits using S3 presigned URLs.

    Uses ``s3_key``, ``s3_bucket``, and ``filename`` from chunk metadata —
    all of which are indexed in OpenSearch and available on ``hit.metadata``
    before serialization redaction runs.

    Keeps the Airtable ``CitationResolver`` untouched; structured hits
    (Airtable lookup results) continue to use Airtable record URLs.
    """

    def __init__(
        self,
        *,
        expiry_seconds: int = 3600,
        aws_region: str = "eu-west-1",
        signing_secret: str | None = None,
        public_base_url: str | None = None,
        link_ttl_seconds: int = 86400,
    ) -> None:
        self._expiry = expiry_seconds
        self._region = aws_region
        # Short-link mode is active only when BOTH a secret and a base URL are
        # configured; otherwise we fall back to raw presigned URLs.
        self._signing_secret = signing_secret
        self._base_url = _sanitize_base_url(public_base_url)
        self._link_ttl = link_ttl_seconds

    @property
    def _short_links(self) -> bool:
        return bool(self._signing_secret and self._base_url)

    def resolve_semantic_hits(self, hits: list[SearchResult]) -> dict[str, Any]:
        """Set ``citation_url`` and ``citations`` on each hit.

        Short-link mode (secret + base_url set): emits ``<base>/cite/<token>`` —
        a tiny opaque link the API resolves to a fresh presigned URL on click.
        Fallback: emits a raw presigned S3 URL directly.
        """
        from pipeline.common.aws import generate_presigned_url  # noqa: PLC0415

        diagnostics: dict[str, Any] = {
            "resolved": 0, "skipped_no_s3": 0, "skipped_derived": 0, "failed": 0,
            "mode": "short_link" if self._short_links else "presigned",
        }
        # key_dir -> resolved original key (or None when the dir has no original)
        dir_cache: dict[str, str | None] = {}

        for h in hits:
            # GUARDRAIL: summary chunks (record_summary / deck_summary) are
            # derived artifacts embedded for recall — they have no single source
            # document and must NEVER carry a citation.
            doc_role = h.metadata.get("doc_role")
            if isinstance(doc_role, str) and doc_role in _DERIVED_DOC_ROLES:
                h.citation_url = None
                h.citations = []
                _clear_source_path(h)
                diagnostics["skipped_derived"] += 1
                continue

            s3_key: str | None = h.metadata.get("s3_key")
            s3_bucket: str | None = h.metadata.get("s3_bucket")
            # Prefer the original key indexed from the sidecar (or backfilled).
            stored_source_key: str | None = h.metadata.get("source_s3_key")

            if not s3_key or not s3_bucket:
                diagnostics["skipped_no_s3"] += 1
                continue

            # Use the stored original key directly — UNLESS it is missing or
            # still points at a derived .txt (older data / fallback), in which
            # case resolve the original by listing the folder.
            candidate = stored_source_key or s3_key
            if candidate.endswith(_DERIVED_TEXT_SUFFIX):
                source_key = _resolve_original_s3_key(
                    candidate,
                    bucket=s3_bucket,
                    region_name=self._region,
                    cache=dir_cache,
                )
            else:
                source_key = candidate

            # GUARDRAIL: citations point at ORIGINAL source documents only. When
            # no original exists (or resolution still lands on a .txt), emit NO
            # citation rather than a link to a derived artifact.
            if source_key is None or source_key.endswith(_DERIVED_TEXT_SUFFIX):
                h.citation_url = None
                h.citations = []
                _clear_source_path(h)
                diagnostics["skipped_derived"] += 1
                continue

            if self._short_links:
                # Tiny opaque link — the /cite endpoint signs the presigned URL
                # on click, so no S3 call is needed here.
                from retrieval.citation_token import make_citation_token  # noqa: PLC0415

                token = make_citation_token(
                    source_key,
                    bucket=s3_bucket,
                    ttl_seconds=self._link_ttl,
                    secret=self._signing_secret,  # type: ignore[arg-type]
                )
                url: str | None = f"{self._base_url}/cite/{token}"
            else:
                url = generate_presigned_url(
                    source_key,
                    bucket=s3_bucket,
                    expiry_seconds=self._expiry,
                    region_name=self._region,
                )

            if url is None:
                diagnostics["failed"] += 1
                continue

            # Surface the resolved ORIGINAL document path in the response. These
            # keys are NOT in _REDACTED_METADATA_KEYS, so they reach the client
            # (unlike the internal s3_key/s3_bucket which stay redacted).
            source_s3_url = f"s3://{s3_bucket}/{source_key}"
            h.metadata["source_s3_key"] = source_key
            h.metadata["source_s3_url"] = source_s3_url

            label = build_semantic_citation_label(h.metadata)
            h.citation_url = url
            h.citations = [
                Citation(
                    cite_id="0",
                    kind="document",
                    label=label,
                    url=url,
                    locator={
                        "source_s3_key": source_key,
                        "source_s3_url": source_s3_url,
                        "primary_key": h.metadata.get("primary_key"),
                        "chunk_id": h.chunk_id,
                        "column_name": h.metadata.get("column_name"),
                        "section_canonical": h.metadata.get("section_canonical"),
                    },
                )
            ]
            diagnostics["resolved"] += 1

        return diagnostics


@dataclass
class SourceCitationContext:
    """Per-source config needed to resolve semantic hits to Airtable URLs."""

    source_name: str
    base_id: str | None
    table_id: str | None
    identifier_field: str | None
    column_slug_map: dict[str, str] = field(default_factory=dict)
    airtable: AirtableSource | None = None


class CitationResolver:
    """Resolve ``record_id`` from email (primary_key) with per-request caching."""

    def __init__(self) -> None:
        self._record_id_cache: dict[tuple[str, str], str | None] = {}

    def clear_cache(self) -> None:
        self._record_id_cache.clear()

    async def resolve_semantic_hits(
        self,
        hits: list[SearchResult],
        ctx: SourceCitationContext,
    ) -> dict[str, Any]:
        """Attach ``citations`` and ``citation_url`` on each semantic hit."""

        diagnostics: dict[str, Any] = {"resolved": 0, "failed": [], "skipped_no_pk": 0}
        if not hits:
            return diagnostics

        unique_pks: list[str] = []
        seen: set[str] = set()
        for h in hits:
            pk = h.metadata.get("primary_key")
            if not pk or not isinstance(pk, str):
                diagnostics["skipped_no_pk"] += 1
                continue
            if pk not in seen:
                seen.add(pk)
                unique_pks.append(pk)

        record_ids: dict[str, str | None] = {}
        display_names: dict[str, str] = {}

        for h in hits:
            pk = h.metadata.get("primary_key")
            if not isinstance(pk, str) or not pk:
                continue
            indexed_rid = h.metadata.get("airtable_record_id")
            if isinstance(indexed_rid, str) and indexed_rid.startswith("rec"):
                record_ids[pk] = indexed_rid

        pks_to_lookup: list[str] = []
        for pk in unique_pks:
            if record_ids.get(pk):
                continue
            cache_key = (ctx.source_name, pk)
            if cache_key in self._record_id_cache:
                record_ids[pk] = self._record_id_cache[cache_key]
                continue
            pks_to_lookup.append(pk)

        if pks_to_lookup:
            batch_ids, batch_fields = await self._resolve_record_ids_batch(pks_to_lookup, ctx)
            record_ids.update(batch_ids)
            for pk, fields in batch_fields.items():
                name = _display_name_from_metadata(fields)
                if name:
                    display_names[pk] = name

        cite_counter = 0
        for h in hits:
            pk = h.metadata.get("primary_key")
            if not isinstance(pk, str) or not pk:
                h.citations = self._citations_without_url(h, ctx, cite_counter)
                cite_counter += len(h.citations)
                continue

            record_id = (
                h.metadata.get("airtable_record_id")
                if isinstance(h.metadata.get("airtable_record_id"), str)
                else None
            ) or record_ids.get(pk)

            url: str | None = None
            if record_id and ctx.base_id and ctx.table_id:
                url = build_airtable_record_url(
                    base_id=ctx.base_id,
                    table_id=ctx.table_id,
                    record_id=record_id,
                )
                diagnostics["resolved"] += 1
            elif pk in record_ids and record_ids[pk] is None:
                if pk not in diagnostics["failed"]:
                    diagnostics["failed"].append(pk)

            label = build_semantic_citation_label(
                h.metadata,
                slug_map=ctx.column_slug_map,
                display_name=display_names.get(pk),
            )
            cite_counter += 1
            main = Citation(
                cite_id=str(cite_counter),
                kind="airtable_record",
                label=label,
                url=url,
                locator={
                    "primary_key": pk,
                    "record_id": record_id,
                    "attachment_column": column_display_name(
                        h.metadata.get("column_name")
                        if isinstance(h.metadata.get("column_name"), str)
                        else None,
                        slug_map=ctx.column_slug_map,
                    ),
                    "section_canonical": h.metadata.get("section_canonical"),
                    "chunk_id": h.chunk_id,
                    "column_name": h.metadata.get("column_name"),
                },
            )
            h.citations = [main]
            h.citation_url = url
            if h.record_id is None and record_id:
                h.record_id = record_id

        return diagnostics

    def citations_for_structured_row(
        self,
        *,
        record_id: str | None,
        citation_url: str | None,
        metadata: dict[str, Any],
        cite_id_start: int = 1,
    ) -> list[Citation]:
        label = _display_name_from_metadata(metadata) or metadata.get("Email") or "Airtable record"
        return [
            Citation(
                cite_id=str(cite_id_start),
                kind="airtable_record",
                label=str(label),
                url=citation_url,
                locator={
                    "record_id": record_id,
                    "primary_key": metadata.get("Email") or metadata.get("primary_key"),
                },
            )
        ]

    async def _resolve_record_ids_batch(
        self,
        primary_keys: list[str],
        ctx: SourceCitationContext,
    ) -> tuple[dict[str, str | None], dict[str, dict[str, Any]]]:
        """Resolve many emails in one or few Airtable calls; returns record_id and row fields per pk."""

        result_ids: dict[str, str | None] = {pk: None for pk in primary_keys}
        result_fields: dict[str, dict[str, Any]] = {}

        if not primary_keys or not ctx.airtable or not ctx.identifier_field:
            return result_ids, result_fields

        id_field = ctx.identifier_field
        fetch_fields = list({id_field, "Display Name"})

        for i in range(0, len(primary_keys), _BATCH_LOOKUP_SIZE):
            batch = primary_keys[i : i + _BATCH_LOOKUP_SIZE]
            formula = build_batch_identifier_formula(id_field, batch)
            if not formula:
                continue
            try:
                rows = await asyncio.to_thread(
                    ctx.airtable._fetch_rows,
                    {
                        "formula": formula,
                        "max_records": len(batch) + 5,
                        "fields": fetch_fields,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "citation_batch_lookup_failed",
                    source=ctx.source_name,
                    batch_size=len(batch),
                    error=str(exc),
                )
                continue

            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                row_fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
                email_val = row_fields.get(id_field)
                if not isinstance(email_val, str) or not email_val.strip():
                    continue
                pk = email_val.strip().lower()
                if pk not in result_ids:
                    continue
                record_id = row.get("id")
                if isinstance(record_id, str) and record_id.startswith("rec"):
                    result_ids[pk] = record_id
                    result_fields[pk] = row_fields
                    self._record_id_cache[(ctx.source_name, pk)] = record_id

        return result_ids, result_fields

    def _citations_without_url(
        self,
        hit: SearchResult,
        ctx: SourceCitationContext,
        start_id: int,
    ) -> list[Citation]:
        label = build_semantic_citation_label(hit.metadata, slug_map=ctx.column_slug_map)
        return [
            Citation(
                cite_id=str(start_id + 1),
                kind="airtable_record",
                label=label,
                url=None,
                locator={"chunk_id": hit.chunk_id},
            )
        ]


def build_source_citation_context(
    *,
    source_name: str,
    base_id: str | None,
    table_id: str | None,
    identifier_field: str | None,
    airtable: AirtableSource | None,
    column_slug_map: dict[str, str] | None = None,
) -> SourceCitationContext:
    return SourceCitationContext(
        source_name=source_name,
        base_id=base_id,
        table_id=table_id,
        identifier_field=identifier_field,
        column_slug_map=column_slug_map or dict(_DEFAULT_COLUMN_SLUG_MAP),
        airtable=airtable,
    )


def assign_cite_ids_to_hits(hits: list[SearchResult]) -> None:
    """Number citations sequentially across hits for synthesis / references."""

    n = 0
    for h in hits:
        updated: list[Citation] = []
        for c in h.citations:
            n += 1
            updated.append(
                Citation(
                    cite_id=str(n),
                    kind=c.kind,
                    label=c.label,
                    url=c.url,
                    locator=dict(c.locator),
                )
            )
        h.citations = updated


def build_markdown_references_section(
    references: list[dict[str, Any]],
    *,
    max_items: int = 40,
) -> str:
    """Markdown block with clickable Airtable links — append to answers for the user."""

    if not references:
        return ""
    lines = ["## References", ""]
    for ref in references[:max_items]:
        cite_id = ref.get("cite_id", "?")
        label = ref.get("label") or "Profile"
        url = ref.get("url")
        if url:
            lines.append(f"- **[{cite_id}]** [{label}]({url})")
        else:
            lines.append(f"- **[{cite_id}]** {label}")
    if len(references) > max_items:
        lines.append(f"\n_Showing {max_items} of {len(references)} sources._")
    return "\n".join(lines)


def append_references_to_answer(
    answer: str | None,
    references: list[dict[str, Any]],
) -> str | None:
    """Ensure synthesized answers end with a References section (server-built links)."""

    block = build_markdown_references_section(references)
    if not block:
        return answer
    text = (answer or "").strip()
    if "## References" in text:
        return text
    return f"{text}\n\n{block}" if text else block


ASSISTANT_CITATION_INSTRUCTION = (
    "CITATIONS ARE MANDATORY AND AUTOMATIC — the user should NEVER have to ask "
    "for them.\n"
    "• EVERY person, document, or record you name in your reply MUST be a "
    "clickable link to its source file, using that hit's citation_url (an S3 "
    "link to the original CV / bio / document).\n"
    "• Write each one inline as a markdown link, e.g. "
    "[Pablo Peña](<citation_url>) — Spanish national …\n"
    "• ALSO paste the full references_markdown section verbatim at the end of "
    "your reply (it already contains the ## References block with every S3 link).\n"
    "• Do NOT summarise sources without linking them, do NOT say 'I can provide "
    "links' — just include them. citation_url is the source-of-truth link; "
    "never invent or omit it."
)


def build_citation_resolve_warning(diagnostics: dict[str, Any]) -> str | None:
    """User-visible summary when citation URL generation failed for some hits."""

    citations_diag = diagnostics.get("citations")
    if not isinstance(citations_diag, dict):
        return None

    # S3 path: diagnostics.citations.s3.failed (integer count)
    s3_diag = citations_diag.get("s3")
    if isinstance(s3_diag, dict):
        s3_failed = s3_diag.get("failed", 0)
        if isinstance(s3_failed, int) and s3_failed > 0:
            return (
                f"{s3_failed} hit(s) missing S3 citation URL "
                "(presigned URL generation failed). See diagnostics.citations.s3."
            )

    # Legacy Airtable path: diagnostics.citations.<source>.failed (list of emails)
    total_failed = 0
    for source_diag in citations_diag.values():
        if isinstance(source_diag, dict):
            failed = source_diag.get("failed")
            if isinstance(failed, list):
                total_failed += len(failed)
    if total_failed > 0:
        return (
            f"{total_failed} profile(s) missing Airtable citation URL "
            "(email lookup failed). See diagnostics.citations."
        )

    return None


def build_references_from_hits(hits: list[SearchResult]) -> list[dict[str, Any]]:
    """Dedupe citations by (url, record_id) for a top-level references block."""

    seen: set[tuple[str | None, str | None]] = set()
    refs: list[dict[str, Any]] = []
    for h in hits:
        for c in h.citations:
            record_id = c.locator.get("record_id") if isinstance(c.locator, dict) else None
            key = (c.url, record_id)
            if key in seen:
                continue
            seen.add(key)
            refs.append(c.to_dict())
    return refs
