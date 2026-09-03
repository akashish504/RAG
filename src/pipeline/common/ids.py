"""ID and hash helpers."""

from __future__ import annotations

import hashlib
import re

from pipeline.embedding_pipeline.models import ChunkType

_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")


def document_hash(body: bytes) -> str:
    """Return sha256 hex digest of the raw source bytes."""

    return hashlib.sha256(body).hexdigest()


def safe_id_part(value: str) -> str:
    """Make a stable ID segment safe for OpenSearch document IDs."""

    cleaned = _SAFE_ID_RE.sub("-", value.strip())
    return cleaned.strip("-") or "unknown"


def chunk_id(
    *,
    document_hash_value: str,
    chunk_type: ChunkType,
    parent_position: int,
    child_position: int | None = None,
) -> str:
    """Build a deterministic chunk ID for idempotent OpenSearch upserts."""

    doc_part = safe_id_part(document_hash_value[:16])
    if chunk_type is ChunkType.PARENT:
        return f"{doc_part}:parent:{parent_position}"

    if child_position is None:
        msg = "child_position is required for child chunks"
        raise ValueError(msg)

    return f"{doc_part}:child:{parent_position}:{child_position}"
