"""Normalization helpers for Airtable -> S3 path building."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from unicodedata import normalize as unicode_normalize

_SAFE_SEGMENT = re.compile(r"[^a-z0-9@._+-]+")
_SAFE_NAME = re.compile(r"[^a-z0-9._-]+")
_MAX_SEGMENT = 200


def slugify_table_name(value: str) -> str:
    raw = unicode_normalize("NFKD", (value or "").strip().lower())
    raw = re.sub(r"\s+", "_", raw)
    raw = _SAFE_NAME.sub("_", raw).strip("._")
    return raw or "unknown_table"


def normalize_identifier(value: str, *, fallback: str = "unknown_identifier") -> str:
    """Normalize Airtable identifier (Email preferred) for S3 path usage."""

    raw = unicode_normalize("NFKD", (value or "").strip().lower())
    raw = _SAFE_SEGMENT.sub("_", raw).strip("._")
    return (raw or fallback)[:_MAX_SEGMENT]


def mask_identifier(value: str) -> str:
    """Mask an identifier for LOG OUTPUT (logs ship to CloudWatch).

    Identifier columns hold PII (profiles_sync uses Email), so log lines show
    only the first two characters. S3 paths keep the full normalized value —
    this is for display, never for key building.
    """
    raw = (value or "").strip()
    if len(raw) <= 2:
        return "…" if raw else "(empty)"
    return f"{raw[:2]}…"


def normalize_facet_value(value: str) -> str:
    """Canonicalise a facet DISPLAY value (not a slug).

    NFKC folds compatibility variants — notably the full-width ampersand "＆"
    (U+FF06) → "&" — so mixed encodings in the source (e.g. "Cities ＆
    Infrastructure" vs "Cities & Infrastructure") store as one canonical value.
    Unlike the slug helpers above, this PRESERVES case and punctuation; it only
    folds Unicode and trims surrounding whitespace.
    """
    return unicode_normalize("NFKC", (value or "")).strip()


def slugify_column_name(value: str) -> str:
    raw = unicode_normalize("NFKD", (value or "").strip().lower())
    raw = re.sub(r"\s+", "_", raw)
    raw = _SAFE_NAME.sub("_", raw).strip("._")
    return raw or "unknown_column"


def sanitize_attachment_filename(original: str | None, *, fallback: str = "attachment") -> str:
    name = unicode_normalize("NFKD", (original or fallback).strip()) or fallback
    name = PurePosixPath(name).name
    stem = PurePosixPath(name).stem
    ext = PurePosixPath(name).suffix.lower()
    stem = _SAFE_NAME.sub("_", stem.lower()).strip("._")[:_MAX_SEGMENT] or fallback
    if ext and len(ext) <= 16 and ext.startswith("."):
        return f"{stem}{ext}"
    return stem
