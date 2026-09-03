"""Citation policy: S3 presigned for documents (placeholder base URL is ignored);
Airtable links only for the explicit airtable_lookup (airtable_only)."""

from __future__ import annotations

from retrieval.citations import S3CitationResolver, _sanitize_base_url, active_citation_mode
from retrieval.models import SearchResult, redact_metadata_for_response


# -- S3 resolver: placeholder / invalid base URL → presigned (not short-link) ----

def test_placeholder_base_url_falls_back_to_presigned():
    for bad in (None, "", "http://<ec2-public-dns>", "<ec2-public-dns>", "localhost:8000"):
        r = S3CitationResolver(signing_secret="secret", public_base_url=bad)
        assert r._base_url is None
        assert r._short_links is False        # → raw presigned S3 URLs


def test_valid_base_url_enables_short_links():
    r = S3CitationResolver(signing_secret="secret", public_base_url="https://mcp.example.com/")
    assert r._base_url == "https://mcp.example.com"
    assert r._short_links is True


def test_sanitize_base_url():
    assert _sanitize_base_url("https://x.com/") == "https://x.com"
    assert _sanitize_base_url("http://<placeholder>") is None
    assert _sanitize_base_url("ftp://x") is None
    assert _sanitize_base_url("  ") is None


def test_active_citation_mode():
    # short-link only when BOTH a secret and a VALID http(s) base URL are present.
    assert active_citation_mode(signing_secret="s", public_base_url="https://api.example.com") \
        == ("short_link", "https://api.example.com")
    # placeholder / missing / no-secret → fragile raw presigned.
    assert active_citation_mode(signing_secret="s", public_base_url="http://<ec2-public-dns>") \
        == ("presigned", None)
    assert active_citation_mode(signing_secret=None, public_base_url="https://api.example.com") \
        == ("presigned", "https://api.example.com")
    assert active_citation_mode(signing_secret="s", public_base_url=None) == ("presigned", None)


# -- No .txt ever reaches the client (GOAL 2) -----------------------------------


def test_redact_drops_txt_source_paths_keeps_real_documents():
    meta = {
        "source_s3_key": "raw/d_quals/1234/1234__record_summary.txt",  # derived → drop
        "source_s3_url": "s3://bucket/raw/d_quals/1234/1234__record_summary.txt",
        "primary_key": "1234",
    }
    out = redact_metadata_for_response(meta)
    assert "source_s3_key" not in out and "source_s3_url" not in out
    assert out["primary_key"] == "1234"  # non-path metadata preserved

    real = {"source_s3_key": "raw/d_quals/1234/att__deck.pptx",
            "source_s3_url": "s3://bucket/raw/d_quals/1234/att__deck.pptx"}
    kept = redact_metadata_for_response(real)
    assert kept["source_s3_key"].endswith(".pptx")   # a genuine source is not stripped
    assert kept["source_s3_url"].endswith(".pptx")


def test_record_summary_hit_carries_no_citation_and_no_txt_path():
    # A record_summary hit is a derived artifact — resolve must null its citation
    # AND clear the .txt locator so no ".txt source" surfaces alongside it.
    hit = SearchResult(
        source="d_quals",
        source_type="semantic",
        score=1.0,
        text="…project summary…",
        chunk_id="abc:child:0:0",
        metadata={
            "doc_role": "record_summary",
            "s3_key": "raw/d_quals/1234/1234__record_summary.txt",
            "s3_bucket": "bucket",
            "source_s3_key": "raw/d_quals/1234/1234__record_summary.txt",
            "source_s3_url": "s3://bucket/raw/d_quals/1234/1234__record_summary.txt",
        },
    )
    diag = S3CitationResolver().resolve_semantic_hits([hit])
    assert diag["skipped_derived"] == 1
    assert hit.citation_url is None and hit.citations == []
    assert "source_s3_key" not in hit.metadata
    assert "source_s3_url" not in hit.metadata
    # Belt-and-suspenders: even the serialised metadata carries no .txt path.
    assert redact_metadata_for_response(hit.metadata).get("source_s3_key") is None


# -- Router strip: S3-only, UNCONDITIONALLY (all modes, incl. airtable_lookup) --

def _strip_structured_citations(hits):
    """Mirror of the router.run() rule (kept in lockstep with router.py).

    PRODUCT DECISION: no Airtable citations, ever — the strip runs
    unconditionally; the AIRTABLE_CITATIONS_ENABLED env flag is inert.
    """
    for h in hits:
        if h.source_type == "structured":
            h.citation_url = None
            h.citations = []
    return hits


def test_airtable_citations_stripped_in_every_mode():
    from types import SimpleNamespace
    struct = SimpleNamespace(source_type="structured",
                             citation_url="https://airtable.com/app/tbl/rec", citations=[1])
    sem = SimpleNamespace(source_type="semantic",
                          citation_url="https://b.s3.amazonaws.com/x.pptx?sig", citations=[])
    _strip_structured_citations([struct, sem])
    assert struct.citation_url is None and struct.citations == []   # airtable citation gone
    assert sem.citation_url.startswith("https://")                  # S3 citation kept


def test_strip_is_unconditional_flag_is_inert():
    # Even a deployment with AIRTABLE_CITATIONS_ENABLED=1 must not leak an
    # airtable.com link — the strip no longer consults the flag.
    from types import SimpleNamespace
    struct = SimpleNamespace(source_type="structured",
                             citation_url="https://airtable.com/app/tbl/rec", citations=[1])
    _strip_structured_citations([struct])
    assert struct.citation_url is None and struct.citations == []


def test_router_strip_has_no_flag_gate():
    # Source-level check that router.py's strip is unconditional: the strip loop
    # must not be guarded by airtable_citations_enabled.
    import inspect
    import retrieval.router as router_mod
    src = inspect.getsource(router_mod.RetrievalRouter.run)
    assert "airtable_citations_enabled" not in src


# -- Adapter: structured rows carry NO airtable link and NO attachment fields ----


def _adapter(long_text=(), attachments=("Attachments",)):
    from types import SimpleNamespace
    from retrieval.sources.airtable import AirtableSource

    src = AirtableSource.__new__(AirtableSource)
    src.name = "d_quals"
    src._cfg = SimpleNamespace(long_text_truncate=800, base_id="appX")
    src._long_text_fields = list(long_text)
    src._attachment_fields = set(attachments)
    src._table_id = "tblY"
    return src


def test_format_rows_no_airtable_citation_and_no_attachments():
    import json

    rows = [{
        "id": "recA",
        "createdTime": "2024-01-01T00:00:00.000Z",
        "fields": {
            "Project Number": "1234",
            "Client Organisation": "Gates Foundation",
            "Attachments": [
                {"url": "https://dl.airtable.com/x/f.pdf", "filename": "f.pdf"}
            ],
        },
    }]
    results, _hints = _adapter()._format_rows(rows)
    r = results[0]
    assert r.citation_url is None and r.citations == []
    assert "Attachments" not in r.metadata
    assert "Attachments" not in r.payload["fields"]
    assert r.metadata["Project Number"] == "1234"          # data preserved
    serialized = json.dumps(r.to_dict(), default=str)
    assert "airtable.com" not in serialized                 # no link of any kind
    assert "dl.airtable.com" not in serialized
