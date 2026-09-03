"""Config loader for Airtable attachment ingestion."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from pipeline.airtable_ingestion.models import (
    AirtableIngestionSettings,
    IngestionDefaults,
    IngestionTargetConfig,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INGESTION_CONFIG_PATH = REPO_ROOT / "config" / "airtable_ingestion.yaml"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing ingestion config: {path}")
    return yaml.safe_load(path.read_text()) or {}


def load_airtable_ingestion_settings(
    *,
    config_path: Path | None = None,
) -> AirtableIngestionSettings:
    """Load settings from YAML + env."""

    path = config_path or Path(
        os.environ.get("AIRTABLE_INGESTION_CONFIG_PATH", DEFAULT_INGESTION_CONFIG_PATH)
    )
    data = _read_yaml(path)

    defaults_raw = data.get("defaults", {}) or {}
    defaults = IngestionDefaults(
        metadata_local_dir=defaults_raw.get("metadata_local_dir", "data/metadata/airtable"),
        metadata_s3_prefix=defaults_raw.get("metadata_s3_prefix", "metadata/airtable_schema"),
        upload_schema_to_s3=bool(defaults_raw.get("upload_schema_to_s3", True)),
        request_timeout_seconds=float(defaults_raw.get("request_timeout_seconds", 120.0)),
        page_size=int(defaults_raw.get("page_size", 100)),
    )

    targets_raw = data.get("targets", {}) or {}
    targets: list[IngestionTargetConfig] = []
    for target_name, raw in targets_raw.items():
        if not isinstance(raw, dict):
            raise TypeError(f"Target {target_name!r} must be a mapping")
        def _norm_name(value: object) -> str | None:
            return str(value) if value and str(value) not in ("null", "None") else None

        normalizer_name = _norm_name(raw.get("normalizer"))

        # Parse per-column normalizer overrides: {column_name: normalizer_name_or_null}
        col_norms_raw: dict[str, object] = raw.get("attachment_column_normalizers") or {}
        attachment_column_normalizers = tuple(
            (str(col), _norm_name(norm)) for col, norm in col_norms_raw.items()
        )

        targets.append(
            IngestionTargetConfig(
                name=target_name,
                enabled=bool(raw.get("enabled", True)),
                poll_enabled=bool(raw.get("poll_enabled", False)),
                allowed_extensions=tuple(
                    "." + str(ext).lower().lstrip(".")
                    for ext in (raw.get("allowed_extensions") or [])
                ),
                database_id=str(raw["database_id"]),
                database_name=str(raw.get("database_name", "")),
                table_id=str(raw["table_id"]),
                table_name=str(raw["table_name"]),
                identifier_column=str(raw["identifier_column"]),
                attachment_columns=tuple(raw.get("attachment_columns", [])),
                text_columns=tuple(raw.get("text_columns", [])),
                metadata_columns=tuple(raw.get("metadata_columns", [])),
                facet_columns=tuple(raw.get("facet_columns", [])),
                s3_prefix=str(raw.get("s3_prefix", "raw")),
                process_images=bool(raw.get("process_images", False)),
                normalizer=normalizer_name,
                attachment_column_normalizers=attachment_column_normalizers,
            )
        )

    pat = (
        os.environ.get("AIRTABLE_PAT_TOKEN")
        or os.environ.get("PAT_TOKEN")
        or os.environ.get("AIRTABLE_API_KEY")
        or ""
    ).strip()
    if not pat:
        raise ValueError("Missing Airtable PAT: set AIRTABLE_PAT_TOKEN in .env")

    s3_bucket = (os.environ.get("S3_BUCKET") or "").strip()
    if not s3_bucket:
        raise ValueError("Missing S3_BUCKET in environment")

    return AirtableIngestionSettings(
        airtable_pat_token=pat,
        aws_region=os.environ.get("AWS_REGION", "eu-west-1"),
        s3_bucket=s3_bucket,
        defaults=defaults,
        targets=tuple(targets),
    )
