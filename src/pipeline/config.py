"""Application configuration.

Loads the YAML defaults in ``config/`` and overlays environment variables.
A single ``Settings`` object is constructed once and passed into pipeline
components, so we never have scattered ``os.getenv`` calls or module-level
globals.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pipeline.embedding_pipeline.reader.tables import TableRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TABLES_PATH = PROJECT_ROOT / "config" / "tables.yaml"
DEFAULT_PIPELINE_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"


@dataclass(frozen=True, slots=True)
class S3Settings:
    bucket: str
    prefix: str
    region: str


@dataclass(frozen=True, slots=True)
class OpenSearchSettings:
    endpoint: str
    index: str
    username: str | None
    password: str | None


@dataclass(frozen=True, slots=True)
class Settings:
    """Top-level settings bundle."""

    env: str
    aws_region: str
    s3: S3Settings
    opensearch: OpenSearchSettings
    pipeline_config: dict[str, Any]
    tables: TableRegistry


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def load_settings(
    *,
    tables_path: Path | None = None,
    pipeline_config_path: Path | None = None,
) -> Settings:
    """Build a Settings object from YAML + environment variables."""

    pipeline_config = _read_yaml(pipeline_config_path or DEFAULT_PIPELINE_CONFIG_PATH)
    s3_cfg = pipeline_config.get("s3", {}) or {}
    os_cfg = pipeline_config.get("opensearch", {}) or {}

    aws_region = os.environ.get("AWS_REGION", "eu-west-1")

    s3 = S3Settings(
        bucket=os.environ.get("S3_BUCKET", s3_cfg.get("bucket", "")),
        prefix=os.environ.get("S3_PREFIX", s3_cfg.get("prefix", "raw/")),
        region=aws_region,
    )

    opensearch = OpenSearchSettings(
        endpoint=os.environ.get("OPENSEARCH_ENDPOINT", os_cfg.get("endpoint", "")),
        index=os.environ.get("OPENSEARCH_INDEX", os_cfg.get("index", "mcp-docs")),
        username=os.environ.get("OPENSEARCH_USERNAME") or None,
        password=os.environ.get("OPENSEARCH_PASSWORD") or None,
    )

    tables = TableRegistry.from_yaml(tables_path or DEFAULT_TABLES_PATH)

    return Settings(
        env=os.environ.get("MCP_ENV", "dev"),
        aws_region=aws_region,
        s3=s3,
        opensearch=opensearch,
        pipeline_config=pipeline_config,
        tables=tables,
    )
