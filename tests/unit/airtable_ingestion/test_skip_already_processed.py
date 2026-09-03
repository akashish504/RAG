"""Tests for the incremental-skip decision in the Airtable ingestion pipeline.

Goal: re-running ingestion must skip attachments that are FULLY processed
(the 150 completed) while re-processing partial failures whose original was
uploaded but whose normalization never finished (the remaining 650).
"""

from __future__ import annotations

from pipeline.airtable_ingestion.pipeline import AirtableAttachmentIngestionPipeline
from pipeline.preprocessing.normalizers.base import AttachmentNormalizer
from pipeline.preprocessing.normalizers.passthrough import PassthroughNormalizer


class _FakeUploader:
    """Uploader whose key_exists answers from a fixed set of present keys."""

    def __init__(self, present: set[str]) -> None:
        self.present = present

    def key_exists(self, *, key: str) -> bool:
        return key in self.present


class _LLMNormalizer:
    """Stand-in for an llm_* normalizer (produces normalized text)."""

    def normalize(self, binary: bytes, filename: str) -> str | None:  # noqa: ARG002
        return "normalized"


def _make_pipeline(present: set[str]) -> AirtableAttachmentIngestionPipeline:
    return AirtableAttachmentIngestionPipeline(
        airtable=object(),  # unused by _already_processed
        uploader=_FakeUploader(present),
        metadata_local_dir="/tmp",
        metadata_s3_prefix="meta",
        upload_schema_to_s3_enabled=False,
        page_size=100,
    )


_ORIG = "raw/kl/doc1/attachments/att123__report.pdf"
_NORM = "raw/kl/doc1/attachments/doc1__normalized.txt"


def test_llm_column_skipped_when_normalized_exists() -> None:
    """Fully processed (normalized text present) → skip."""
    pipe = _make_pipeline(present={_ORIG, _NORM})
    done, marker = pipe._already_processed(
        col_normalizer=_LLMNormalizer(), original_key=_ORIG, normalized_key=_NORM
    )
    assert done is True
    assert marker == "normalized text"


def test_llm_column_reprocessed_when_only_original_exists() -> None:
    """Partial failure: original uploaded, normalization never finished → re-process."""
    pipe = _make_pipeline(present={_ORIG})  # normalized missing
    done, _ = pipe._already_processed(
        col_normalizer=_LLMNormalizer(), original_key=_ORIG, normalized_key=_NORM
    )
    assert done is False, "orphan original must be re-processed, not skipped"


def test_llm_column_processed_when_nothing_exists() -> None:
    """Brand new attachment → process."""
    pipe = _make_pipeline(present=set())
    done, _ = pipe._already_processed(
        col_normalizer=_LLMNormalizer(), original_key=_ORIG, normalized_key=_NORM
    )
    assert done is False


def test_passthrough_column_skipped_when_original_exists() -> None:
    """Passthrough produces no normalized text → original presence is the marker."""
    pipe = _make_pipeline(present={_ORIG})  # no normalized ever expected
    done, marker = pipe._already_processed(
        col_normalizer=PassthroughNormalizer(), original_key=_ORIG, normalized_key=_NORM
    )
    assert done is True
    assert marker == "original in S3"


def test_passthrough_column_processed_when_original_missing() -> None:
    pipe = _make_pipeline(present=set())
    done, _ = pipe._already_processed(
        col_normalizer=PassthroughNormalizer(), original_key=_ORIG, normalized_key=_NORM
    )
    assert done is False


def test_normalizer_satisfies_protocol() -> None:
    """Sanity: the test LLM normalizer is a valid AttachmentNormalizer."""
    assert isinstance(_LLMNormalizer(), AttachmentNormalizer)
