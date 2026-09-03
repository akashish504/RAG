"""Guard: the embed pipeline indexes ONLY normalized .txt for d_quals — never a
raw binary (which would bypass extraction/summaries/facets)."""

from __future__ import annotations

from pipeline.embedding_pipeline.reader.document_loader import DocumentLoader
from pipeline.embedding_pipeline.reader.tables import TableRegistry


def _d_quals():
    return TableRegistry.from_yaml("config/tables.yaml").get("d_quals")


def test_normalized_txt_and_summaries_are_supported() -> None:
    t = _d_quals()
    assert DocumentLoader._is_supported(t, "raw/d.quals/1010012/col/att1/att1__normalized.txt")
    assert DocumentLoader._is_supported(t, "raw/d.quals/1010012/__record_summary.txt")


def test_every_raw_binary_is_rejected() -> None:
    t = _d_quals()
    for ext in (".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xlsm"):
        key = f"raw/d.quals/1010012/deliverable_attachments/att1/deck{ext}"
        assert not DocumentLoader._is_supported(t, key), f"{ext} must NOT be embeddable"


def test_warn_helper_runs_for_binary(caplog) -> None:
    # Should not raise; emits a warning for an un-normalized binary.
    DocumentLoader._warn_unnormalized_binary(_d_quals(), "raw/d.quals/p/c/deck.pdf")


def test_knowledge_library_is_also_txt_only() -> None:
    # KL mirrors d_quals: embed normalized .txt only, never raw binaries.
    kl = TableRegistry.from_yaml("config/tables.yaml").get("knowledge_library")
    assert DocumentLoader._is_supported(kl, "raw/knowledge_library/Some Brief/c/att1__normalized.txt")
    for ext in (".pdf", ".pptx", ".docx", ".xlsx"):
        assert not DocumentLoader._is_supported(kl, f"raw/knowledge_library/x/c/att{ext}")


def test_knowledge_library_facets_in_mapping() -> None:
    from pipeline.embedding_pipeline.indexer.mappings import INDEX_MAPPING
    props = INDEX_MAPPING["properties"]
    for f in ("kd_type", "country_region", "author", "team", "item_type", "client", "language"):
        assert props[f] == {"type": "keyword"}, f
    assert props["date_of_publication"]["type"] == "date"
