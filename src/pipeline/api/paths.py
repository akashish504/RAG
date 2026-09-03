"""Repository paths shared by the API package."""

from __future__ import annotations

from pathlib import Path

# src/pipeline/api/paths.py → repo root is four levels up
REPO_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_DALBERG_PROFILES_SCHEMA_PATH = REPO_ROOT / "config" / "dalberg_profiles_schema.json"
