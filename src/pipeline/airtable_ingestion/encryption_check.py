"""Detect password-protected attachments before they enter the pipeline.

Positive detection only: a file is reported as protected ONLY when encryption
can be affirmatively proven. Corrupt, truncated, or unrecognized bytes return
``False`` and flow to the existing downstream failure handling — this gate
must never eat a file the extractors could have read.

Coverage:

* **PDF** — pypdf's ``is_encrypted``. PDFs whose *user* password is empty
  (owner-password-only files that open without a prompt, e.g. edit-restricted
  reports) decrypt with ``""`` and remain machine-readable, so they are let
  through.
* **OOXML Office files** (.docx/.pptx/.xlsx/.xlsm) and legacy **.doc/.ppt** —
  password-protected OOXML documents are re-wrapped in a CFB (OLE compound)
  container holding ``EncryptionInfo``/``EncryptedPackage`` streams. Stream
  names are stored UTF-16LE in the CFB directory, so a plain substring scan
  is a reliable positive signal with no extra dependency. Legacy binary
  encryption (pre-2007 ``.doc``/``.ppt`` password schemes without those
  streams) is not detected here and takes the normal failure path instead.
* **Images** — no password concept; never checked.
"""

from __future__ import annotations

import io
from pathlib import Path

_PDF_MAGIC = b"%PDF"
_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Stream names inside an encrypted Office CFB container, UTF-16LE-encoded as
# they appear in the CFB directory sectors. Directory names are always
# null-terminated and null-padded to 64 bytes, so requiring trailing nulls
# rules out the same words appearing as UTF-16 *text content* in a legacy
# .doc/.ppt body.
_ENCRYPTION_INFO_UTF16 = "EncryptionInfo".encode("utf-16-le") + b"\x00" * 4
_ENCRYPTED_PACKAGE_UTF16 = "EncryptedPackage".encode("utf-16-le") + b"\x00" * 4

_PDF_EXTENSIONS = frozenset({".pdf"})
_OFFICE_EXTENSIONS = frozenset({".docx", ".pptx", ".xlsx", ".xlsm", ".doc", ".ppt"})


def is_password_protected(binary: bytes, file_name: str) -> bool:
    """Return True only when ``binary`` is provably password-protected."""
    ext = Path(file_name).suffix.lower()
    if ext in _PDF_EXTENSIONS:
        return _pdf_is_locked(binary)
    if ext in _OFFICE_EXTENSIONS:
        return _office_is_encrypted(binary)
    return False


def _pdf_is_locked(binary: bytes) -> bool:
    # The PDF header may sit anywhere in the first 1024 bytes (per spec).
    if _PDF_MAGIC not in binary[:1024]:
        return False
    try:
        from pypdf import PdfReader  # noqa: PLC0415
    except ImportError:
        return False
    try:
        reader = PdfReader(io.BytesIO(binary))
        if not reader.is_encrypted:
            return False
    except Exception:  # noqa: BLE001
        return False
    try:
        # Empty user password (owner-only protection) → readable, allow it.
        return not reader.decrypt("")
    except Exception:  # noqa: BLE001
        # Provably encrypted but undecryptable (unsupported cipher, missing
        # crypto backend) — nothing downstream could read it either.
        return True


def _office_is_encrypted(binary: bytes) -> bool:
    if not binary.startswith(_CFB_MAGIC):
        # OOXML zips (PK…), corrupt files, plain legacy .doc/.ppt without
        # encryption streams: not provably protected.
        return False
    return (
        _ENCRYPTION_INFO_UTF16 in binary
        or _ENCRYPTED_PACKAGE_UTF16 in binary
    )
