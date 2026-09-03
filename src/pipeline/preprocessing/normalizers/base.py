"""AttachmentNormalizer — the contract every normalizer must satisfy.

A normalizer transforms raw attachment bytes into clean plain text that
the embedding pipeline can chunk directly via TextParser.  Returning
``None`` signals the ingestion pipeline to upload the original binary
unchanged (safe passthrough fallback).

To add a new normalizer:
    1. Create a new module in this package implementing this Protocol.
    2. Register it in ``registry.py`` under a unique name string.
    3. Set ``normalizer: <name>`` on the target in ``airtable_ingestion.yaml``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AttachmentNormalizer(Protocol):
    """Normalize raw attachment bytes to clean plain text, or None to keep original.

    Implementations MUST be fail-safe: any exception should be caught internally
    and ``None`` returned so the ingestion pipeline uploads the original binary.
    """

    def normalize(self, binary: bytes, filename: str) -> str | None:
        """Return normalized UTF-8 plain text, or None to upload original binary.

        Args:
            binary:   Raw file bytes downloaded from Airtable / S3.
            filename: Original filename (e.g. ``"cv.docx"``), used to infer type.

        Returns:
            Plain-text string to store as ``{attachment_id}__normalized.txt``,
            or ``None`` if this normalizer does not apply to the given file.
        """
        ...
