"""Convert PPTX bytes to PDF bytes using LibreOffice headless.

Returns None when LibreOffice is not available so callers can gracefully
fall back to python-pptx text extraction.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Large/complex decks (20-30 MB) are slow to convert, especially under --workers
# CPU contention. Generous cap so big decks finish instead of timing out; override
# with LIBREOFFICE_TIMEOUT_S if needed.
_LIBREOFFICE_TIMEOUT_S = int(os.environ.get("LIBREOFFICE_TIMEOUT_S", "300"))
_LIBREOFFICE_BIN = "libreoffice"


def pptx_to_pdf_bytes(pptx_bytes: bytes) -> bytes | None:
    """Convert PPTX binary to PDF using LibreOffice headless.

    Returns PDF bytes on success, or None if LibreOffice is not installed /
    conversion fails.  None triggers the python-pptx text-extraction fallback
    in llm_content.
    """
    if shutil.which(_LIBREOFFICE_BIN) is None:
        log.debug("pptx_to_pdf: libreoffice not on PATH; returning None")
        return None

    # LibreOffice headless occasionally crashes (uno::RuntimeException / exit 1)
    # transiently. Retry once with a fresh isolated profile before giving up, so
    # fewer decks degrade to a raw upload over a one-off crash.
    for attempt in (1, 2):
        pdf = _convert_once(pptx_bytes)
        if pdf is not None:
            return pdf
        if attempt == 1:
            log.info("pptx_to_pdf: conversion failed — retrying once")
    return None


def _convert_once(pptx_bytes: bytes) -> bytes | None:
    """One LibreOffice conversion attempt with a private throwaway profile."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pptx_path = Path(tmpdir) / "input.pptx"
        pptx_path.write_bytes(pptx_bytes)

        # Give LibreOffice a private, throwaway user profile per conversion. The
        # default shared profile (~/.config/libreoffice) is the usual source of
        # the "com::sun::star::uno::RuntimeException / exit 1" crash — a lock
        # collision or a stale lock left by a prior/concurrent soffice. An
        # isolated profile per call avoids both.
        profile_dir = Path(tmpdir) / "lo_profile"
        try:
            result = subprocess.run(
                [
                    _LIBREOFFICE_BIN,
                    f"-env:UserInstallation=file://{profile_dir}",
                    "--headless",
                    "--convert-to", "pdf",
                    "--outdir", tmpdir,
                    str(pptx_path),
                ],
                capture_output=True,
                timeout=_LIBREOFFICE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            log.warning("pptx_to_pdf: libreoffice timed out after %ds", _LIBREOFFICE_TIMEOUT_S)
            return None
        except OSError as exc:
            log.warning("pptx_to_pdf: could not launch libreoffice: %s", exc)
            return None

        if result.returncode != 0:
            log.warning(
                "pptx_to_pdf: libreoffice exit %d — %s",
                result.returncode,
                result.stderr.decode(errors="replace")[:300],
            )
            return None

        pdf_path = Path(tmpdir) / "input.pdf"
        if not pdf_path.is_file():
            log.warning("pptx_to_pdf: expected output %s not found", pdf_path)
            return None

        return pdf_path.read_bytes()
