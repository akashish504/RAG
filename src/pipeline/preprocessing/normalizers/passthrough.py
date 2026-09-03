"""PassthroughNormalizer — no-op, always returns None (keep original binary)."""

from __future__ import annotations


class PassthroughNormalizer:
    """Default normalizer that performs no transformation.

    Used when a target has ``normalizer: null`` (or no normalizer set).
    The ingestion pipeline uploads the original binary unchanged.
    """

    def normalize(self, binary: bytes, filename: str) -> str | None:  # noqa: ARG002
        return None
