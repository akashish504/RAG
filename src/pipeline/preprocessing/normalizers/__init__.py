"""Attachment normalizers for the ingestion preprocessing layer."""

from pipeline.preprocessing.normalizers.base import AttachmentNormalizer
from pipeline.preprocessing.normalizers.passthrough import PassthroughNormalizer
from pipeline.preprocessing.normalizers.registry import get_normalizer, registered_names

__all__ = [
    "AttachmentNormalizer",
    "PassthroughNormalizer",
    "get_normalizer",
    "registered_names",
    # Concrete normalizers imported lazily via registry; do not import directly here
    # to keep the package importable without anthropic/docx dependencies installed.
]
