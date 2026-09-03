"""Repo-root and config paths used by the retrieval module."""

from __future__ import annotations

from pathlib import Path

# src/retrieval/paths.py -> repo root is three levels up.
REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_RETRIEVAL_SOURCES_PATH = REPO_ROOT / "config" / "retrieval_sources.yaml"
DEFAULT_PIPELINE_CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"
