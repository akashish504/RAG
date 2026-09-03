"""Table -> S3 mapping and per-table chunking config.

The registry is config-driven (``config/tables.yaml`` by default). Adding a
new table requires no code changes: just add an entry to the YAML file with
its S3 prefix and any chunking overrides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pipeline.embedding_pipeline.chunker.base import ChunkingConfig


@dataclass(frozen=True, slots=True)
class TableConfig:
    """Configuration for one logical table."""

    name: str
    s3_prefix: str
    # OpenSearch index name for this table.  Defaults to mcp-{name} with
    # underscores replaced by hyphens (e.g. dalberg_profiles → mcp-dalberg-profiles).
    # Each table has its own index so datasets remain isolated when new tables
    # go live — no migration of existing data required.
    index_name: str = ""
    description: str | None = None
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    supported_extensions: tuple[str, ...] = ()
    chunker_strategy: str = "parent_child"


class TableRegistry:
    """In-memory registry of table configs."""

    def __init__(self, tables: list[TableConfig]) -> None:
        if not tables:
            msg = "TableRegistry requires at least one table"
            raise ValueError(msg)
        self._by_name: dict[str, TableConfig] = {t.name: t for t in tables}
        # Longest prefix wins, so nested prefixes resolve correctly.
        self._by_prefix: list[TableConfig] = sorted(tables, key=lambda t: -len(t.s3_prefix))

    @classmethod
    def from_yaml(cls, path: str | Path) -> "TableRegistry":
        data = yaml.safe_load(Path(path).read_text())
        return cls.from_mapping(data or {})

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "TableRegistry":
        defaults = data.get("default", {}) or {}
        default_chunking_dict: dict[str, Any] = defaults.get("chunking", {}) or {}
        default_extensions: tuple[str, ...] = tuple(
            ext.lower() for ext in defaults.get("supported_extensions", []) or []
        )
        default_chunker_strategy: str = defaults.get("chunker_strategy", "parent_child")

        raw_tables = data.get("tables", {}) or {}
        if not raw_tables:
            msg = "tables section is empty"
            raise ValueError(msg)

        tables: list[TableConfig] = []
        for name, cfg in raw_tables.items():
            if not isinstance(cfg, dict):
                msg = f"table {name!r} config must be a mapping"
                raise TypeError(msg)
            if "s3_prefix" not in cfg:
                msg = f"table {name!r} is missing required key 's3_prefix'"
                raise KeyError(msg)

            chunking_overrides = cfg.get("chunking", {}) or {}
            merged_chunking = {**default_chunking_dict, **chunking_overrides}
            chunking = ChunkingConfig(**merged_chunking)

            extensions = tuple(
                ext.lower()
                for ext in cfg.get("supported_extensions", default_extensions)
            )

            chunker_strategy = cfg.get("chunker_strategy", default_chunker_strategy)

            # index_name: explicit in YAML or computed as mcp-{table-name}.
            raw_index_name = cfg.get("index_name") or ""
            index_name = raw_index_name or f"mcp-{name.replace('_', '-')}"

            tables.append(
                TableConfig(
                    name=name,
                    s3_prefix=cfg["s3_prefix"],
                    index_name=index_name,
                    description=cfg.get("description"),
                    chunking=chunking,
                    supported_extensions=extensions,
                    chunker_strategy=chunker_strategy,
                )
            )
        return cls(tables)

    def get(self, name: str) -> TableConfig:
        if name not in self._by_name:
            msg = f"Unknown table: {name!r}. Known: {sorted(self._by_name)}"
            raise KeyError(msg)
        return self._by_name[name]

    def resolve_from_key(self, s3_key: str) -> TableConfig | None:
        for table in self._by_prefix:
            if s3_key.startswith(table.s3_prefix):
                return table
        return None

    def list(self) -> list[TableConfig]:
        return list(self._by_name.values())

    def names(self) -> list[str]:
        return sorted(self._by_name)
